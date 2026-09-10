"use client";

import { Panel } from "./Panel";
import styles from "./WorkStatusPanel.module.css";
import type {
  IntakeChoiceAction,
  IntakeChoiceResponse,
  IntakeNextStepView,
  NaturalLanguageRouteView,
} from "@/lib/orchestrator";
import {
  candidateExplanationsFromRoute,
  candidateSourceLabel,
} from "@/lib/orchestrator/candidate-explanations";
import { useOrchestratorStore } from "@/stores/orchestrator-store";

const STATE_LABEL: Record<string, string> = {
  created: "대기",
  planning: "계획 중",
  awaiting_approval: "승인 대기",
  awaiting_user_input: "추가 정보 대기",
  running: "실행 중",
  completed: "완료",
  failed: "실패",
};

const SUBTASK_KIND_LABEL: Record<string, string> = {
  mechanical: "기구",
  organic: "유기 형상",
};

const SUBTASK_STATUS_LABEL: Record<string, string> = {
  planned: "계획됨",
  running: "진행 중",
  completed: "완료",
  failed: "실패",
};

function routeStatus(route: NaturalLanguageRouteView | null): {
  label: string;
  detail: string;
} | null {
  if (!route) return null;
  const intake = route.intake_decision;
  if (intake?.status === "runtime_catalog_match") {
    return {
      label: "기존 검증 후보 사용",
      detail: "새 초안 없음 · 시각/출시 검토 필요",
    };
  }
  if (intake?.status === "draft_queue_match") {
    return {
      label: "기존 초안 재사용 후보",
      detail: "새 초안 없음 · 재사용/수정 검토 필요",
    };
  }
  if (intake?.status === "duplicate_or_similar_candidate") {
    return {
      label: "유사 후보 검토 필요",
      detail: "새 초안 없음 · 재사용/수정 선택 필요",
    };
  }
  if (intake?.status === "new_draft_allowed") {
    return {
      label: "새 초안 생성 가능",
      detail: "기존 후보 없음 · 검토 대기 경로",
    };
  }
  if (route.selected_route === "curated_asset") {
    return {
      label: "검증된 후보 경로",
      detail: "최종 시각/출시 검토 필요",
    };
  }
  if (route.selected_route === "draft_asset") {
    return {
      label: "에셋 초안 검토 대기",
      detail: "제품 카탈로그 자동 등록 없음",
    };
  }
  if (route.selected_route === "ask_user") {
    return {
      label: "추가 정보 필요",
      detail: route.clarification_questions[0] ?? "질문에 답하면 다시 분류",
    };
  }
  return {
    label: "경로 분류됨",
    detail: "추가 검토 필요",
  };
}

function intakeAction(route: NaturalLanguageRouteView | null): {
  action: IntakeChoiceAction;
  label: string;
  detail: string;
} | null {
  if (!route) return null;
  const intake = route.intake_decision;
  if (intake?.status === "runtime_catalog_match") {
    return {
      action: "use_existing_runtime_asset",
      label: "기존 후보 사용",
      detail: "새 초안을 만들지 않고 검증 후보를 사용합니다.",
    };
  }
  if (intake?.status === "draft_queue_match") {
    return {
      action: "review_existing_draft",
      label: "기존 초안 검토",
      detail: "기존 초안을 재사용하거나 수정할지 검토합니다.",
    };
  }
  if (intake?.status === "duplicate_or_similar_candidate") {
    return {
      action: "adapt_similar_candidate",
      label: "유사 후보 검토",
      detail: "유사 후보를 수정 대상으로 검토합니다.",
    };
  }
  if (intake?.status === "new_draft_allowed") {
    return {
      action: "create_new_draft",
      label: "새 초안 요청",
      detail: "검토 대기 상태의 새 초안 경로를 엽니다.",
    };
  }
  if (route.selected_route === "ask_user") {
    return {
      action: "ask_for_more_info",
      label: "추가 정보 필요로 기록",
      detail: "질문 답변은 대화 입력에서 별도로 진행합니다.",
    };
  }
  return null;
}

function nextStepLabel(step: IntakeNextStepView): string {
  if (step.status === "runtime_asset_selected") return "기존 후보 선택됨";
  if (step.status === "draft_review_handoff") return "기존 초안 검토 단계";
  if (step.status === "similar_candidate_review") return "유사 후보 검토 단계";
  if (step.status === "draft_authoring_started") return "새 초안 생성됨";
  if (step.status === "information_needed") return "추가 정보 대기 유지";
  return "다음 경계 기록됨";
}

function choiceLabel(choice: IntakeChoiceResponse): string {
  if (choice.action === "use_existing_runtime_asset") return "기존 후보 사용 기록됨";
  if (choice.action === "review_existing_draft") return "기존 초안 검토 기록됨";
  if (choice.action === "adapt_similar_candidate") return "유사 후보 검토 기록됨";
  if (choice.action === "create_new_draft") return "새 초안 요청 기록됨";
  if (choice.action === "ask_for_more_info") return "추가 정보 필요 기록됨";
  return "선택 기록됨";
}

export function WorkStatusPanel(): JSX.Element {
  const sessionState = useOrchestratorStore((s) => s.sessionState);
  const sessionId = useOrchestratorStore((s) => s.sessionId);
  const subtasks = useOrchestratorStore((s) => s.subtasks);
  const subtaskStatuses = useOrchestratorStore((s) => s.subtaskStatuses);
  const latestRouteResult = useOrchestratorStore((s) => s.latestRouteResult);
  const latestIntakeChoice = useOrchestratorStore((s) => s.latestIntakeChoice);
  const latestIntakeNextStep = useOrchestratorStore((s) => s.latestIntakeNextStep);
  const pendingIntakeChoice = useOrchestratorStore((s) => s.pendingIntakeChoice);
  const pendingIntakeNextStep = useOrchestratorStore((s) => s.pendingIntakeNextStep);
  const pendingRuntimeAssetExecution = useOrchestratorStore(
    (s) => s.pendingRuntimeAssetExecution,
  );
  const chooseIntakeAction = useOrchestratorStore((s) => s.chooseIntakeAction);
  const advanceIntakeNextStep = useOrchestratorStore((s) => s.advanceIntakeNextStep);
  const executeRuntimeAsset = useOrchestratorStore((s) => s.executeRuntimeAsset);

  const label = sessionState ? STATE_LABEL[sessionState] ?? sessionState : "—";
  const route = routeStatus(latestRouteResult);
  const action = intakeAction(latestRouteResult);
  const candidateExplanations = candidateExplanationsFromRoute(
    latestRouteResult,
  ).slice(0, 2);

  return (
    <Panel title="작업 상태" testId="work-status-panel">
      <div className={styles.row}>
        <span className={styles.k}>상태</span>
        <span
          className={`${styles.badge} ${styles[`state_${sessionState ?? "none"}`] ?? ""}`}
          data-testid="session-state-badge"
        >
          {label}
        </span>
      </div>
      <div className={styles.row}>
        <span className={styles.k}>세션</span>
        <span className={styles.v} data-testid="session-id">
          {sessionId ?? "—"}
        </span>
      </div>
      {route ? (
        <div className={styles.routeBox} data-testid="route-status">
          <span className={styles.routeLabel}>{route.label}</span>
          <span className={styles.routeDetail}>{route.detail}</span>
          {candidateExplanations.length > 0 ? (
            <div
              className={styles.candidateList}
              data-testid="candidate-explanations"
            >
              {candidateExplanations.map((explanation, index) => (
                <div
                  className={styles.candidateItem}
                  data-testid="candidate-explanation"
                  key={`${explanation.source}-${index}`}
                >
                  <span className={styles.candidateLabel}>
                    {candidateSourceLabel(explanation.source)}
                  </span>
                  <span className={styles.routeDetail}>
                    {explanation.display_text_ko}
                  </span>
                </div>
              ))}
              <span className={styles.routeDetail}>
                제품 품질/출시 검토는 별도입니다.
              </span>
            </div>
          ) : null}
          {latestIntakeNextStep ? (
            <div className={styles.choiceBox} data-testid="intake-next-step-panel">
              <span className={styles.routeLabel}>
                {nextStepLabel(latestIntakeNextStep)}
              </span>
              <span className={styles.routeDetail}>
                {latestIntakeNextStep.message}
              </span>
              <span className={styles.routeDetail}>
                제품 품질/출시 검토는 아직 필요합니다.
              </span>
              {latestIntakeNextStep.status === "runtime_asset_selected" ? (
                latestIntakeNextStep.runtime_execution_started ? (
                  <span className={styles.routeDetail}>
                    제조 검증 산출물이 생성되었습니다. 최종 검토 전 상태입니다.
                  </span>
                ) : (
                  <button
                    className={styles.choiceButton}
                    type="button"
                    disabled={pendingRuntimeAssetExecution}
                    onClick={() => void executeRuntimeAsset()}
                    data-testid="runtime-asset-execution"
                  >
                    {pendingRuntimeAssetExecution
                      ? "제조 검증 중..."
                      : "제조 검증 실행"}
                  </button>
                )
              ) : null}
            </div>
          ) : latestIntakeChoice ? (
            <div className={styles.choiceBox} data-testid="intake-choice-recorded">
              <span className={styles.routeLabel}>
                {choiceLabel(latestIntakeChoice)}
              </span>
              <button
                className={styles.choiceButton}
                type="button"
                disabled={pendingIntakeNextStep}
                onClick={() => void advanceIntakeNextStep()}
                data-testid="intake-next-step"
              >
                {pendingIntakeNextStep
                  ? "확인 중..."
                  : latestRouteResult?.intake_decision?.metadata
                        .capable_model_route === true
                    ? "검토 대기 초안 생성(모델 호출)"
                    : "다음 경계 확인"}
              </button>
              <span className={styles.routeDetail}>
                CAD/STL/Orca 실행이나 출시 승인은 아직 시작하지 않습니다.
              </span>
            </div>
          ) : action ? (
            <div className={styles.choiceBox} data-testid="intake-choice-panel">
              <button
                className={styles.choiceButton}
                type="button"
                disabled={pendingIntakeChoice}
                onClick={() => void chooseIntakeAction(action.action)}
                data-testid={`intake-choice-${action.action}`}
              >
                {pendingIntakeChoice ? "기록 중..." : action.label}
              </button>
              <span className={styles.routeDetail}>{action.detail}</span>
            </div>
          ) : null}
        </div>
      ) : null}
      <div className={styles.subtasks}>
        <h3 className={styles.subhead}>하위 작업</h3>
        {subtasks.length === 0 ? (
          <p className={styles.empty}>아직 계획된 하위 작업이 없습니다.</p>
        ) : (
          <ul className={styles.list}>
            {subtasks.map((task) => {
              const status = subtaskStatuses[task.id] ?? "planned";
              const statusClass = styles[`status_${status}`] ?? "";
              return (
                <li key={task.id} className={styles.subtask}>
                  <span className={styles.kind}>
                    {SUBTASK_KIND_LABEL[task.kind] ?? task.kind}
                  </span>
                  <span
                    className={`${styles.kind} ${statusClass}`}
                    data-testid={`subtask-status-${task.id}`}
                    data-subtask-status={status}
                  >
                    {SUBTASK_STATUS_LABEL[status]}
                  </span>
                  <span>{task.description}</span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Panel>
  );
}
