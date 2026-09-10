/**
 * Zustand store backing the workbench (Phase 7).
 *
 * Layering:
 *  - The orchestrator API is the source of truth for session state,
 *    pending gates, and subtasks. The store reflects the latest
 *    server response and the live WS event stream.
 *  - We do NOT mirror the whole server state — only the projection
 *    the UI panels render (Conversation, Approval, EventFeed,
 *    Approval card).
 *
 * Phase 6 contract reminders (mirrored as guards here):
 *  - `/chat` must not be sent while the session is awaiting approval,
 *    running, or in a terminal state. `isChatDisabled` collapses
 *    every reason chat is blocked into a single boolean for the UI.
 *  - Approve/Reject buttons must disable while a decision is in
 *    flight (`pendingApproval`).
 */

import { create } from "zustand";

import {
  type ApprovalDecision,
  type ArtifactKind,
  type ClarificationAnswerValue,
  type ChatResponse,
  type ClarifyResponse,
  type IntakeChoiceAction,
  type IntakeChoiceResponse,
  type IntakeNextStepView,
  type OrchestratorClient,
  type PendingGateView,
  type PendingClarificationView,
  type NaturalLanguageRouteView,
  type RuntimeAssetExecutionResponse,
  type SessionCreateResponse,
  type SessionEvent,
  type SessionState,
  type SessionStateView,
  type SubtaskCompletedPayload,
  type SubtaskView,
  createOrchestratorClient,
  isChatLockedState,
} from "@/lib/orchestrator";

export type ChatRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  createdAt: string;
}

export interface ArtifactEntry {
  /** Deduplication key: subtask_id + kind + relative_uri. */
  id: string;
  /** Display label — typically the filename. */
  label: string;
  /** Absolute URL for fetching via the `/artifacts` route. */
  url: string;
  kind: ArtifactKind;
  sessionId: string;
  subtaskId: string;
  /** Manifest-recorded relative URI (within the subtask sandbox). */
  relativeUri: string;
  /** Manifest artifact metadata. Used for visual quality and audit hints. */
  metadata?: Record<string, unknown>;
}

export type SubtaskStatus = "planned" | "running" | "completed" | "failed";

interface OrchestratorActions {
  /** Replace the active client (used by tests). */
  setClient(client: OrchestratorClient): void;
  createSession(label?: string): Promise<void>;
  sendChat(message: string): Promise<void>;
  answerClarification(answerText: string): Promise<void>;
  chooseIntakeAction(action: IntakeChoiceAction): Promise<void>;
  advanceIntakeNextStep(): Promise<void>;
  executeRuntimeAsset(): Promise<void>;
  approveGate(decision: ApprovalDecision, comments?: string): Promise<void>;
  ingestEvent(event: SessionEvent): void;
  hydrateFromState(snapshot: SessionStateView): void;
  reset(): void;
}

export interface OrchestratorStoreState extends OrchestratorActions {
  client: OrchestratorClient;
  sessionId: string | null;
  traceId: string | null;
  sessionState: SessionState | null;
  messages: readonly ChatMessage[];
  events: readonly SessionEvent[];
  subtasks: readonly SubtaskView[];
  subtaskStatuses: Readonly<Record<string, SubtaskStatus>>;
  pendingGate: PendingGateView | null;
  pendingClarification: PendingClarificationView | null;
  latestRouteResult: NaturalLanguageRouteView | null;
  latestIntakeChoice: IntakeChoiceResponse | null;
  latestIntakeNextStep: IntakeNextStepView | null;
  artifacts: readonly ArtifactEntry[];
  pendingApproval: boolean;
  pendingIntakeChoice: boolean;
  pendingIntakeNextStep: boolean;
  pendingRuntimeAssetExecution: boolean;
  sendingChat: boolean;
  sendingClarification: boolean;
  creatingSession: boolean;
  lastError: string | null;
}

const INITIAL_STATE: Omit<
  OrchestratorStoreState,
  | "client"
  | "setClient"
  | "createSession"
  | "sendChat"
  | "answerClarification"
  | "chooseIntakeAction"
  | "advanceIntakeNextStep"
  | "executeRuntimeAsset"
  | "approveGate"
  | "ingestEvent"
  | "hydrateFromState"
  | "reset"
> = {
  sessionId: null,
  traceId: null,
  sessionState: null,
  messages: [],
  events: [],
  subtasks: [],
  subtaskStatuses: {},
  pendingGate: null,
  pendingClarification: null,
  latestRouteResult: null,
  latestIntakeChoice: null,
  latestIntakeNextStep: null,
  artifacts: [],
  pendingApproval: false,
  pendingIntakeChoice: false,
  pendingIntakeNextStep: false,
  pendingRuntimeAssetExecution: false,
  sendingChat: false,
  sendingClarification: false,
  creatingSession: false,
  lastError: null,
};

function pickPendingGate(
  gates: readonly PendingGateView[],
): PendingGateView | null {
  return gates.find((gate) => gate.status === "pending") ?? null;
}

function nextMessageId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

function applyChatResponse(
  state: OrchestratorStoreState,
  response: ChatResponse,
): Partial<OrchestratorStoreState> {
  // fallback ADR item 6: a pre-filter refusal must be checked FIRST. A blocked
  // reply carries no route result, no clarifying questions and no subtasks, so
  // every branch below it falls through to the generic planner-failure text —
  // which would report a deliberate safety refusal as a broken planner and
  // throw away the rule table's `user_message_ko`.
  //
  // The session state in that reply is still `created`, so the composer stays
  // enabled and a corrected prompt can be sent straight away.
  const prefilterMessage = response.prefilter?.user_message_ko;
  const assistantText = prefilterMessage
    ? prefilterMessage
    : response.route_result
    ? formatRouteMessage(response.route_result)
    : response.clarifying_questions.length
    ? response.clarifying_questions.join("\n")
    : response.subtasks.length
      ? `하위 작업 ${response.subtasks.length}개가 계획되었습니다.`
      : "실행할 수 있는 계획을 만들지 못했습니다.";

  const messages: ChatMessage[] = [
    ...state.messages,
    {
      id: nextMessageId("assistant"),
      role: "assistant",
      text: assistantText,
      createdAt: new Date().toISOString(),
    },
  ];

  return {
    sessionState: response.state,
    traceId: response.trace_id,
    subtasks: response.subtasks,
    subtaskStatuses: Object.fromEntries(
      response.subtasks.map((task) => [task.id, "planned"] as const),
    ),
    pendingGate: response.pending_gate,
    pendingClarification: response.pending_clarification ?? null,
    latestRouteResult: response.route_result ?? null,
    latestIntakeChoice: null,
    latestIntakeNextStep: null,
    messages,
    sendingChat: false,
    lastError: null,
  };
}

function formatClarificationMessage(
  clarification: PendingClarificationView,
): string {
  return clarification.questions
    .slice(0, 3)
    .map((question) => question.question)
    .join("\n");
}

function answersFromFreeText(
  clarification: PendingClarificationView,
  text: string,
): Record<string, ClarificationAnswerValue> {
  return Object.fromEntries(
    clarification.questions
      .filter((question) => question.required)
      .map((question) => [question.id, text]),
  );
}

function applyClarifyResponse(
  state: OrchestratorStoreState,
  response: ClarifyResponse,
): Partial<OrchestratorStoreState> {
  const resolvedRouteClarification =
    response.state === "created" &&
    state.pendingClarification?.subtask_id == null &&
    response.route_result != null;
  const resolvedText = response.pending_clarification
    ? "답변을 확인했습니다. 주제를 하나로 좁혀 주세요."
    : resolvedRouteClarification
      ? "추가 정보를 확인했습니다. 다시 안전한 경로로 분류했습니다."
      : "추가 정보를 확인했습니다. 작업을 이어서 진행합니다.";
  const messages: ChatMessage[] = [
    ...state.messages,
    {
      id: nextMessageId("assistant-clarification-resolved"),
      role: "assistant",
      text: resolvedText,
      createdAt: new Date().toISOString(),
    },
  ];
  return {
    sessionState: advanceSessionState(state.sessionState, response.state),
    pendingClarification: response.pending_clarification ?? null,
    latestRouteResult: response.route_result ?? state.latestRouteResult,
    sendingClarification: false,
    messages,
    lastError: null,
  };
}

function applyIntakeChoiceResponse(
  state: OrchestratorStoreState,
  response: IntakeChoiceResponse,
): Partial<OrchestratorStoreState> {
  const messages: ChatMessage[] = [
    ...state.messages,
    {
      id: nextMessageId("assistant-intake-choice"),
      role: "assistant",
      text: [
        response.message,
        "아직 제품 품질이나 출시 가능 상태는 아닙니다.",
      ].join("\n"),
      createdAt: new Date().toISOString(),
    },
  ];
  return {
    sessionState: advanceSessionState(state.sessionState, response.state),
    pendingIntakeChoice: false,
    latestIntakeChoice: response,
    latestIntakeNextStep: null,
    messages,
    lastError: null,
  };
}

function applyIntakeNextStepResponse(
  state: OrchestratorStoreState,
  response: IntakeNextStepView,
): Partial<OrchestratorStoreState> {
  const messages: ChatMessage[] = [
    ...state.messages,
    {
      id: nextMessageId("assistant-intake-next-step"),
      role: "assistant",
      text: [
        response.message,
        "CAD/STL/Orca 실행은 시작하지 않았고, 시각 품질과 출시 검토는 별도로 남아 있습니다.",
      ].join("\n"),
      createdAt: new Date().toISOString(),
    },
  ];
  return {
    sessionState: advanceSessionState(state.sessionState, response.state),
    pendingIntakeNextStep: false,
    latestIntakeNextStep: response,
    messages,
    lastError: null,
  };
}

function applyRuntimeAssetExecutionResponse(
  state: OrchestratorStoreState,
  response: RuntimeAssetExecutionResponse,
): Partial<OrchestratorStoreState> {
  const messages: ChatMessage[] = [
    ...state.messages,
    {
      id: nextMessageId("assistant-runtime-asset"),
      role: "assistant",
      text: [
        response.message,
        "G-code 생성은 제조 검증 결과이며, 제품 품질/출시 승인은 아직 아닙니다.",
      ].join("\n"),
      createdAt: new Date().toISOString(),
    },
  ];
  return {
    sessionState: advanceSessionState(state.sessionState, response.state),
    latestIntakeNextStep: response.next_step,
    pendingRuntimeAssetExecution: false,
    messages,
    lastError: null,
  };
}

function formatRouteMessage(route: NaturalLanguageRouteView): string {
  const intake = route.intake_decision;
  if (intake?.status === "runtime_catalog_match") {
    return [
      "기존 검증 후보를 찾았습니다.",
      "새 에셋 초안은 만들지 않고 후보 경로로 표시합니다.",
      "최종 시각 품질과 출시 검토는 아직 필요합니다.",
    ].join("\n");
  }
  if (intake?.status === "draft_queue_match") {
    return [
      "기존 에셋 초안을 찾았습니다.",
      "새 초안은 만들지 않고 재사용 또는 수정 검토 후보로 표시합니다.",
      "제품 카탈로그에 자동 등록되지 않습니다.",
    ].join("\n");
  }
  if (intake?.status === "duplicate_or_similar_candidate") {
    return [
      "유사한 에셋 후보가 있습니다.",
      "새 초안은 만들지 않고 재사용 또는 수정 검토가 필요합니다.",
    ].join("\n");
  }
  if (intake?.status === "new_draft_allowed") {
    return [
      "기존 후보가 없어 새 에셋 초안 경로로 분류되었습니다.",
      "검토 대기 상태이며 제품 카탈로그에 자동 등록되지 않습니다.",
    ].join("\n");
  }
  if (route.selected_route === "curated_asset") {
    return [
      "검증된 후보 경로로 분류되었습니다.",
      "최종 시각 품질과 출시 검토는 아직 필요합니다.",
    ].join("\n");
  }
  if (route.selected_route === "draft_asset") {
    return [
      "새 에셋 초안 생성/검토 대기 경로입니다.",
      "검토 대기 상태이며 제품 카탈로그에 자동 등록되지 않습니다.",
    ].join("\n");
  }
  if (route.selected_route === "ask_user") {
    return "추가 정보가 필요합니다.";
  }
  return "요청을 안전한 생성 경로로 분류했습니다.";
}

function buildGCodeCompletedMessage(
  filename: string,
  visualReviewRequired: boolean,
): ChatMessage {
  const reviewNote = visualReviewRequired
    ? "\n다만 요청한 형상처럼 보이는지 시각 품질 검토가 필요합니다."
    : "\n파라미터 설정 혹은 출력을 지시해주세요.";
  return {
    id: nextMessageId("assistant-gcode"),
    role: "assistant",
    text: `G-code 변환이 완료되었습니다.\n${filename} 파일이 생성되었습니다.${reviewNote}`,
    createdAt: new Date().toISOString(),
  };
}

export const useOrchestratorStore = create<OrchestratorStoreState>(
  (set, get) => ({
    ...INITIAL_STATE,
    client: createOrchestratorClient(),

    setClient(client) {
      set({ client });
    },

    reset() {
      set({ ...INITIAL_STATE });
    },

    hydrateFromState(snapshot: SessionStateView) {
      set({
        sessionId: snapshot.session_id,
        traceId: snapshot.trace_id,
        sessionState: snapshot.state,
        subtasks: snapshot.subtasks,
        subtaskStatuses: Object.fromEntries(
          snapshot.subtasks.map((task) => [task.id, "planned"] as const),
        ),
        pendingGate: pickPendingGate(snapshot.pending_gates),
        pendingClarification: snapshot.pending_clarification ?? null,
        latestRouteResult: snapshot.latest_route_result ?? null,
        latestIntakeChoice: snapshot.latest_intake_choice ?? null,
        latestIntakeNextStep: snapshot.latest_intake_next_step ?? null,
      });
    },

    async createSession(label?: string) {
      set({ creatingSession: true, lastError: null });
      try {
        const response: SessionCreateResponse =
          await get().client.createSession(label ? { client_label: label } : {});
        set({
          sessionId: response.session_id,
          traceId: response.trace_id,
          sessionState: response.state,
          messages: [],
          events: [],
          subtasks: [],
          subtaskStatuses: {},
          pendingGate: null,
          pendingClarification: null,
          latestRouteResult: null,
          latestIntakeChoice: null,
          latestIntakeNextStep: null,
          artifacts: [],
          pendingApproval: false,
          pendingIntakeChoice: false,
          pendingIntakeNextStep: false,
          pendingRuntimeAssetExecution: false,
          sendingChat: false,
          sendingClarification: false,
          creatingSession: false,
          lastError: null,
        });
      } catch (err) {
        set({
          creatingSession: false,
          lastError: err instanceof Error ? err.message : String(err),
        });
        throw err;
      }
    },

    async sendChat(message: string) {
      const { sessionId, sessionState, sendingChat } = get();
      if (!sessionId) throw new Error("먼저 세션을 시작해주세요.");
      if (sendingChat) return; // de-dupe rapid double-submits
      if (isChatLockedState(sessionState)) {
        // Hard guard: matches Phase 6 server-side 409 invariants.
        throw new Error(
          `현재 세션 상태(${sessionState})에서는 새 메시지를 보낼 수 없습니다.`,
        );
      }
      const trimmed = message.trim();
      if (!trimmed) return;

      const userMessage: ChatMessage = {
        id: nextMessageId("user"),
        role: "user",
        text: trimmed,
        createdAt: new Date().toISOString(),
      };
      set((prev) => ({
        messages: [...prev.messages, userMessage],
        sendingChat: true,
        lastError: null,
      }));

      try {
        const response = await get().client.sendChat({
          session_id: sessionId,
          message: trimmed,
        });
        set((prev) => applyChatResponse(prev, response));
      } catch (err) {
        set({
          sendingChat: false,
          lastError: err instanceof Error ? err.message : String(err),
        });
        throw err;
      }
    },

    async answerClarification(answerText: string) {
      const {
        sessionId,
        pendingClarification,
        sendingClarification,
      } = get();
      if (!sessionId || !pendingClarification) {
        throw new Error("처리할 추가 정보 요청이 없습니다.");
      }
      if (sendingClarification) return;
      const trimmed = answerText.trim();
      if (!trimmed) return;

      const userMessage: ChatMessage = {
        id: nextMessageId("user-clarification"),
        role: "user",
        text: trimmed,
        createdAt: new Date().toISOString(),
      };
      set((prev) => ({
        messages: [...prev.messages, userMessage],
        sendingClarification: true,
        lastError: null,
      }));

      try {
        const response = await get().client.clarify({
          session_id: sessionId,
          clarification_id: pendingClarification.clarification_id,
          answers: answersFromFreeText(pendingClarification, trimmed),
        });
        set((prev) => applyClarifyResponse(prev, response));
      } catch (err) {
        set({
          sendingClarification: false,
          lastError: err instanceof Error ? err.message : String(err),
        });
        throw err;
      }
    },

    async chooseIntakeAction(action: IntakeChoiceAction) {
      const { sessionId, latestRouteResult, pendingIntakeChoice } = get();
      if (!sessionId || !latestRouteResult) {
        throw new Error("선택할 intake 경로가 없습니다.");
      }
      if (pendingIntakeChoice) return;

      set({ pendingIntakeChoice: true, lastError: null });
      try {
        const response = await get().client.chooseIntakeAction({
          session_id: sessionId,
          action,
          route_snapshot_id: latestRouteResult.route_snapshot_id,
        });
        set((prev) => applyIntakeChoiceResponse(prev, response));
      } catch (err) {
        set({
          pendingIntakeChoice: false,
          lastError: err instanceof Error ? err.message : String(err),
        });
        throw err;
      }
    },

    async advanceIntakeNextStep() {
      const {
        sessionId,
        latestRouteResult,
        latestIntakeChoice,
        latestIntakeNextStep,
        pendingIntakeNextStep,
      } = get();
      if (!sessionId || !latestRouteResult || !latestIntakeChoice) {
        throw new Error("다음 경계로 넘길 intake 선택이 없습니다.");
      }
      if (latestIntakeNextStep || pendingIntakeNextStep) return;

      set({ pendingIntakeNextStep: true, lastError: null });
      try {
        const response = await get().client.advanceIntakeNextStep({
          session_id: sessionId,
          route_snapshot_id: latestRouteResult.route_snapshot_id,
        });
        set((prev) => applyIntakeNextStepResponse(prev, response));
      } catch (err) {
        set({
          pendingIntakeNextStep: false,
          lastError: err instanceof Error ? err.message : String(err),
        });
        throw err;
      }
    },

    async executeRuntimeAsset() {
      const {
        sessionId,
        traceId,
        latestRouteResult,
        latestIntakeNextStep,
        pendingRuntimeAssetExecution,
      } = get();
      if (!sessionId || !latestRouteResult || !latestIntakeNextStep) {
        throw new Error("실행할 런타임 에셋 선택 경계가 없습니다.");
      }
      if (latestIntakeNextStep.status !== "runtime_asset_selected") {
        throw new Error("검증된 후보가 선택된 상태에서만 제조 검증을 실행할 수 있습니다.");
      }
      if (latestIntakeNextStep.runtime_execution_started) return;
      if (pendingRuntimeAssetExecution) return;

      set({ pendingRuntimeAssetExecution: true, lastError: null });
      try {
        const response = await get().client.executeRuntimeAsset({
          session_id: sessionId,
          route_snapshot_id: latestRouteResult.route_snapshot_id,
        });
        set((prev) => applyRuntimeAssetExecutionResponse(prev, response));
        get().ingestEvent({
          session_id: response.session_id,
          trace_id: traceId ?? "",
          kind: "subtask_completed",
          state: response.state,
          occurred_at: new Date().toISOString(),
          payload: {
            subtask_id: response.subtask_id,
            kind: "decorative_asset",
            manifest: response.manifest,
          },
        });
      } catch (err) {
        set({
          pendingRuntimeAssetExecution: false,
          lastError: err instanceof Error ? err.message : String(err),
        });
        throw err;
      }
    },

    async approveGate(decision: ApprovalDecision, comments?: string) {
      const { sessionId, pendingGate, pendingApproval } = get();
      if (!sessionId || !pendingGate) {
        throw new Error("처리할 승인 요청이 없습니다.");
      }
      if (pendingApproval) return;

      set({ pendingApproval: true, lastError: null });
      try {
        const response = await get().client.approveGate({
          session_id: sessionId,
          gate_id: pendingGate.gate_id,
          decision,
          comments: comments ?? null,
        });
        set((prev) => ({
          sessionState: advanceSessionState(prev.sessionState, response.state),
          pendingGate:
            prev.pendingGate?.gate_id === response.gate_id
              ? { ...prev.pendingGate, status: decision }
              : prev.pendingGate,
          pendingApproval: false,
        }));
      } catch (err) {
        set({
          pendingApproval: false,
          lastError: err instanceof Error ? err.message : String(err),
        });
        throw err;
      }
    },

    ingestEvent(event: SessionEvent) {
      set((prev) => {
        if (prev.sessionId && event.session_id !== prev.sessionId) {
          return prev;
        }

        const events = [...prev.events, event].slice(-200);
        let pendingGate = prev.pendingGate;
        let pendingClarification = prev.pendingClarification;
        let latestRouteResult = prev.latestRouteResult;
        let latestIntakeChoice = prev.latestIntakeChoice;
        let latestIntakeNextStep = prev.latestIntakeNextStep;
        let subtasks = prev.subtasks;
        let subtaskStatuses = prev.subtaskStatuses;
        let artifacts = prev.artifacts;
        let messages = prev.messages;
        if (event.kind === "approval_requested") {
          const payload = event.payload as Partial<PendingGateView>;
          if (payload.gate_id && payload.prompt && payload.requested_at) {
            pendingGate = {
              gate_id: payload.gate_id,
              prompt: payload.prompt,
              requested_at: payload.requested_at,
              status: "pending",
            };
          }
        } else if (event.kind === "approval_resolved") {
          const payload = event.payload as {
            gate_id?: string;
            decision?: ApprovalDecision;
          };
          if (
            payload.gate_id &&
            payload.decision &&
            pendingGate?.gate_id === payload.gate_id
          ) {
            pendingGate = { ...pendingGate, status: payload.decision };
          }
        } else if (event.kind === "natural_language_route_selected") {
          latestRouteResult = event.payload as unknown as NaturalLanguageRouteView;
          latestIntakeChoice = null;
          latestIntakeNextStep = null;
        } else if (event.kind === "intake_choice_recorded") {
          latestIntakeChoice = event.payload as unknown as IntakeChoiceResponse;
          latestIntakeNextStep = null;
        } else if (event.kind === "intake_next_step_recorded") {
          latestIntakeNextStep = event.payload as unknown as IntakeNextStepView;
        } else if (event.kind === "clarification_requested") {
          const clarification =
            event.payload as unknown as PendingClarificationView;
          pendingClarification = clarification;
          messages = [
            ...messages,
            {
              id: nextMessageId("assistant-clarification"),
              role: "assistant",
              text: formatClarificationMessage(clarification),
              createdAt: new Date().toISOString(),
            },
          ];
        } else if (event.kind === "clarification_resolved") {
          pendingClarification = null;
        } else if (event.kind === "planning_completed") {
          const payload = event.payload as { subtasks?: SubtaskView[] };
          if (Array.isArray(payload.subtasks)) {
            subtasks = payload.subtasks;
            subtaskStatuses = Object.fromEntries(
              payload.subtasks.map((task) => [task.id, "planned"] as const),
            );
          }
        } else if (event.kind === "subtask_started") {
          const payload = event.payload as { subtask_id?: string };
          if (payload.subtask_id) {
            subtaskStatuses = {
              ...subtaskStatuses,
              [payload.subtask_id]: "running",
            };
          }
        } else if (event.kind === "subtask_completed") {
          // Phase 9D: read manifest ADR manifest.artifacts[].relative_uri.
          // Legacy flat keys (stl_path/gcode_path/organic_mesh_path) are
          // ignored in favor of the manifest; this keeps the store source
          // of truth aligned with /artifacts route + future replay.
          const payload = event.payload as unknown as SubtaskCompletedPayload;
          if (payload.subtask_id) {
            subtaskStatuses = {
              ...subtaskStatuses,
              [payload.subtask_id]: "completed",
            };
          }
          const manifest = payload.manifest;
          if (
            manifest &&
            manifest.status === "success" &&
            Array.isArray(manifest.artifacts)
          ) {
            const baseUrl = prev.client.baseUrl.replace(/\/+$/, "");
            const sessionId = event.session_id;
            const subtaskId = payload.subtask_id;
            const seen = new Set(artifacts.map((a) => a.id));
            const additions: ArtifactEntry[] = [];
            const gcodeMessages: ChatMessage[] = [];
            const visualReviewRequired = manifest.artifacts.some(
              (ref) =>
                ref.status === "ok" &&
                ref.metadata?.visual_quality_required === true &&
                [
                  "review_required",
                  "not_evaluated",
                  "manual_pass_candidate",
                  "manual_fail",
                  "automated_fail",
                ].includes(
                  typeof ref.metadata.visual_quality_status === "string"
                    ? ref.metadata.visual_quality_status
                    : "review_required",
                ),
            );
            for (const ref of manifest.artifacts) {
              if (ref.status !== "ok") continue;
              const id = `${subtaskId}:${ref.kind}:${ref.relative_uri}`;
              if (seen.has(id)) continue;
              seen.add(id);
              const filename =
                ref.relative_uri.split("/").pop() ?? ref.relative_uri;
              const url = `${baseUrl}/${ref.relative_uri
                .split("/")
                .map(encodeURIComponent)
                .join("/")}`;
              additions.push({
                id,
                label: filename,
                url,
                kind: ref.kind,
                sessionId,
                subtaskId,
                relativeUri: ref.relative_uri,
                metadata: ref.metadata ?? {},
              });
              if (ref.kind === "gcode") {
                gcodeMessages.push(
                  buildGCodeCompletedMessage(filename, visualReviewRequired),
                );
              }
            }
            if (additions.length > 0) {
              artifacts = [...artifacts, ...additions];
            }
            if (gcodeMessages.length > 0) {
              messages = [...messages, ...gcodeMessages];
            }
          }
        } else if (event.kind === "subtask_failed") {
          const payload = event.payload as { subtask_id?: string };
          if (payload.subtask_id) {
            subtaskStatuses = {
              ...subtaskStatuses,
              [payload.subtask_id]: "failed",
            };
          }
        }
        let sessionState =
          event.kind === "clarification_requested"
            ? "awaiting_user_input"
            : advanceSessionState(prev.sessionState, event.state);
        if (
          event.kind !== "clarification_resolved" &&
          pendingClarification
        ) {
          sessionState = "awaiting_user_input";
        }

        return {
          events,
          sessionState,
          pendingGate,
          pendingClarification,
          latestRouteResult,
          latestIntakeChoice,
          latestIntakeNextStep,
          subtasks,
          subtaskStatuses,
          artifacts,
          messages,
        };
      });
    },
  }),
);

/* ----- Selectors ---------------------------------------------------- */

/**
 * Returns the preferred preview mesh. Mechanical STL is preferred because it
 * is the manufacturable slice input; organic OBJ is the fallback preview when
 * no mechanical mesh exists.
 */
export function selectPreferredMeshArtifact(
  state: OrchestratorStoreState,
): ArtifactEntry | null {
  for (let i = state.artifacts.length - 1; i >= 0; i--) {
    const a = state.artifacts[i];
    if (a.kind === "mechanical_mesh") return a;
  }
  for (let i = state.artifacts.length - 1; i >= 0; i--) {
    const a = state.artifacts[i];
    if (a.kind === "organic_mesh") return a;
  }
  return null;
}

export const selectLatestMeshArtifact = selectPreferredMeshArtifact;

export function selectIsChatDisabled(state: OrchestratorStoreState): boolean {
  if (state.creatingSession || state.sendingChat || state.sendingClarification) {
    return true;
  }
  if (state.pendingRuntimeAssetExecution) return true;
  if (!state.sessionId) return true;
  if (
    state.sessionState === "awaiting_user_input" &&
    state.pendingClarification
  ) {
    return false;
  }
  return isChatLockedState(state.sessionState);
}

export function selectChatDisabledReason(
  state: OrchestratorStoreState,
): string | null {
  if (!state.sessionId) return "세션을 시작하면 채팅할 수 있습니다.";
  if (state.creatingSession) return "세션을 만드는 중입니다…";
  if (state.sendingChat) return "메시지를 보내는 중입니다…";
  if (state.sendingClarification) return "답변을 보내는 중입니다…";
  if (state.pendingRuntimeAssetExecution) return "제조 검증을 실행하는 중입니다…";
  if (
    state.sessionState === "awaiting_user_input" &&
    state.pendingClarification
  ) {
    return "추가 정보에 답하면 작업을 이어갑니다.";
  }
  if (!isChatLockedState(state.sessionState)) return null;
  switch (state.sessionState) {
    case "awaiting_user_input":
      return "추가 정보가 필요합니다.";
    case "awaiting_approval":
      return "승인 대기 중입니다. 승인 또는 거절을 선택해주세요.";
    case "running":
      return "파이프라인이 실행 중입니다.";
    case "completed":
      return "세션이 완료되었습니다. 계속하려면 새 세션을 시작해주세요.";
    case "failed":
      return "세션이 실패했습니다. 다시 시도하려면 새 세션을 시작해주세요.";
    default:
      return "잠시 채팅을 사용할 수 없습니다.";
  }
}

const SESSION_STATE_RANK: Record<SessionState, number> = {
  created: 0,
  planning: 1,
  awaiting_user_input: 3,
  awaiting_approval: 2,
  running: 3,
  completed: 4,
  failed: 4,
};

function advanceSessionState(
  current: SessionState | null,
  next: SessionState,
): SessionState {
  if (!current) return next;
  if (current === "running" && next === "awaiting_user_input") return next;
  if (current === "awaiting_user_input" && next === "running") return next;
  if (current === "awaiting_user_input" && next === "created") return next;
  if (SESSION_STATE_RANK[next] >= SESSION_STATE_RANK[current]) return next;
  return current;
}
