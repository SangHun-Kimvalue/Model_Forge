"use client";

import { useEffect, useMemo, useRef } from "react";

import { Panel } from "./Panel";
import styles from "./EventFeed.module.css";
import {
  candidateExplanationsFromIntakeDecision,
  candidateSourceLabel,
} from "@/lib/orchestrator/candidate-explanations";
import { formatSubtaskFailureDetail } from "@/lib/orchestrator/semantic-failure";
import { useOrchestratorStore } from "@/stores/orchestrator-store";
import type { SessionEvent, SessionEventKind, SessionState } from "@/lib/orchestrator";

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString();
}

const EVENT_LABEL: Record<SessionEventKind, string> = {
  state_changed: "상태 변경",
  natural_language_route_selected: "자연어 경로 분류",
  intake_choice_recorded: "경로 선택 기록",
  intake_next_step_recorded: "다음 경계 기록",
  planning_completed: "계획 완료",
  subtask_started: "하위 작업 시작",
  subtask_progress: "진행 상황",
  subtask_completed: "하위 작업 완료",
  subtask_failed: "하위 작업 실패",
  approval_requested: "승인 요청",
  approval_resolved: "승인 처리",
  clarification_requested: "추가 정보 요청",
  clarification_resolved: "추가 정보 확인",
  error: "오류",
};

const STATE_LABEL: Record<SessionState, string> = {
  created: "대기",
  planning: "계획 중",
  awaiting_approval: "승인 대기",
  awaiting_user_input: "추가 정보 대기",
  running: "실행 중",
  completed: "완료",
  failed: "실패",
};

function eventDetail(event: SessionEvent): string {
  const payload = event.payload as Record<string, unknown>;
  if (event.kind === "state_changed") {
    const from = typeof payload.from === "string" ? payload.from : undefined;
    const to = typeof payload.to === "string" ? payload.to : undefined;
    if (from && to) return `${from} → ${to}`;
  }
  if (event.kind === "planning_completed") {
    return `하위 작업 ${String(payload.subtask_count ?? 0)}개`;
  }
  if (event.kind === "natural_language_route_selected") {
    const route =
      typeof payload.selected_route === "string"
        ? payload.selected_route
        : "unknown";
    const reason =
      typeof payload.reason === "string" ? payload.reason : "route_selected";
    const questions = Array.isArray(payload.clarification_questions)
      ? payload.clarification_questions
          .filter((item): item is string => typeof item === "string")
          .slice(0, 3)
      : [];
    const generation =
      payload.generation_allowed === true
        ? "후보 생성 가능"
        : "생성 전 대기";
    const intake = formatIntakeDecision(payload.intake_decision);
    const explanation = formatCandidateExplanation(payload.intake_decision);
    const details = [
      `${route} / ${reason}`,
      generation,
      intake,
      explanation,
      ...questions,
    ].filter((item): item is string => Boolean(item));
    return details.join(" / ");
  }
  if (event.kind === "intake_choice_recorded") {
    const action = typeof payload.action === "string" ? payload.action : "";
    const message =
      typeof payload.message === "string" ? payload.message : "선택을 기록했습니다.";
    const flags = [
      payload.runtime_execution_started === true ? "런타임 실행됨" : "런타임 미실행",
      payload.runtime_catalog_registered === true ? "카탈로그 등록됨" : "카탈로그 미등록",
      payload.release_allowed === true ? "출시 가능" : "출시 미승인",
    ];
    return [formatIntakeAction(action), message, ...flags]
      .filter((item) => item.length > 0)
      .join(" / ");
  }
  if (event.kind === "intake_next_step_recorded") {
    const status = typeof payload.status === "string" ? payload.status : "";
    const message =
      typeof payload.message === "string" ? payload.message : "다음 경계를 기록했습니다.";
    const flags = [
      payload.runtime_execution_started === true ? "런타임 실행됨" : "런타임 미실행",
      payload.runtime_catalog_registered === true ? "카탈로그 등록됨" : "카탈로그 미등록",
      payload.release_allowed === true ? "출시 가능" : "출시 미승인",
    ];
    return [formatNextStepStatus(status), message, ...flags]
      .filter((item) => item.length > 0)
      .join(" / ");
  }
  if (event.kind === "subtask_started" || event.kind === "subtask_completed") {
    const kind = typeof payload.kind === "string" ? payload.kind : "unknown";
    const subtaskId =
      typeof payload.subtask_id === "string" ? payload.subtask_id : "unknown";
    return `${kind} / ${subtaskId}`;
  }
  if (event.kind === "subtask_progress") {
    const message =
      typeof payload.message === "string" ? payload.message : "작업 진행 중";
    if (payload.stage === "template_fallback") {
      const detail = objectRecord(payload.detail);
      const templateId =
        typeof detail?.template_id === "string"
          ? detail.template_id
          : typeof payload.template_id === "string"
            ? payload.template_id
            : undefined;
      const params = formatTemplateParams(
        detail?.template_params ?? payload.template_params,
      );
      const details = [
        message,
        templateId ? `템플릿: ${templateId}` : undefined,
        params ? `파라미터: ${params}` : undefined,
      ].filter((item): item is string => Boolean(item));
      return details.join(" / ");
    }
    const status =
      payload.status === "started"
        ? "시작"
        : payload.status === "completed"
          ? "완료"
          : undefined;
    return status ? `${message} (${status})` : message;
  }
  if (event.kind === "subtask_failed") {
    return formatSubtaskFailureDetail(payload);
  }
  if (event.kind === "approval_requested") {
    const gateId = typeof payload.gate_id === "string" ? payload.gate_id : "gate";
    return gateId;
  }
  if (event.kind === "approval_resolved") {
    const decision =
      payload.decision === "approved"
        ? "승인"
        : payload.decision === "rejected"
          ? "거절"
          : "처리";
    return decision;
  }
  if (event.kind === "clarification_requested") {
    const questions = Array.isArray(payload.questions) ? payload.questions : [];
    const text = questions
      .map((item) =>
        typeof item === "object" &&
        item !== null &&
        "question" in item &&
        typeof item.question === "string"
          ? item.question
          : null,
      )
      .filter((item): item is string => item !== null)
      .slice(0, 3)
      .join(" / ");
    return text || "추가 정보가 필요합니다.";
  }
  if (event.kind === "clarification_resolved") {
    return "답변을 확인했습니다.";
  }
  if (event.kind === "error") {
    return typeof payload.error_type === "string" ? payload.error_type : "오류";
  }
  return "";
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function formatIntakeDecision(value: unknown): string {
  const intake = objectRecord(value);
  const status = typeof intake?.status === "string" ? intake.status : undefined;
  if (status === "runtime_catalog_match") {
    return "기존 검증 후보 사용, 새 초안 없음";
  }
  if (status === "draft_queue_match") {
    return "기존 초안 재사용/수정 검토, 새 초안 없음";
  }
  if (status === "duplicate_or_similar_candidate") {
    return "유사 후보 검토 필요, 새 초안 없음";
  }
  if (status === "new_draft_allowed") {
    return "기존 후보 없음, 새 초안 경로 가능";
  }
  return "";
}

function formatCandidateExplanation(value: unknown): string {
  const explanation = candidateExplanationsFromIntakeDecision(value)[0];
  if (!explanation) return "";
  return `${candidateSourceLabel(explanation.source)}: ${explanation.display_text_ko}`;
}

function formatIntakeAction(action: string): string {
  if (action === "use_existing_runtime_asset") return "기존 후보 사용";
  if (action === "review_existing_draft") return "기존 초안 검토";
  if (action === "adapt_similar_candidate") return "유사 후보 수정 검토";
  if (action === "create_new_draft") return "새 초안 요청";
  if (action === "ask_for_more_info") return "추가 정보 요청";
  return "";
}

function formatNextStepStatus(status: string): string {
  if (status === "runtime_asset_selected") return "기존 후보 선택됨";
  if (status === "draft_review_handoff") return "기존 초안 검토";
  if (status === "similar_candidate_review") return "유사 후보 검토";
  if (status === "draft_authoring_started") return "새 초안 생성";
  if (status === "information_needed") return "추가 정보 필요";
  return "";
}

function formatTemplateParams(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  return Object.entries(value as Record<string, unknown>)
    .filter(([, param]) =>
      ["string", "number", "boolean"].includes(typeof param),
    )
    .slice(0, 6)
    .map(([key, param]) => `${key}=${String(param)}`)
    .join(", ");
}

export function EventFeed(): JSX.Element {
  const events = useOrchestratorStore((s) => s.events);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Pre-format timestamps so toLocaleTimeString() doesn't run every render cycle.
  const formattedEvents = useMemo(
    () =>
      events.map((e) => ({
        ...e,
        detail: eventDetail(e),
        formattedTime: formatTime(e.occurred_at),
      })),
    [events],
  );

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  return (
    <Panel title="이벤트 로그" testId="event-feed">
      <div className={styles.list} ref={scrollRef} data-testid="event-list">
        {formattedEvents.length === 0 ? (
          <p className={styles.empty}>아직 수신된 이벤트가 없습니다…</p>
        ) : (
          formattedEvents.map((event, idx) => (
            <div
              key={`${event.occurred_at}-${idx}`}
              className={styles.event}
              data-testid="event-row"
            >
              <span className={styles.kind}>
                {EVENT_LABEL[event.kind] ?? event.kind}
              </span>
              <span className={styles.detail}>{event.detail}</span>
              <span className={styles.sessionState}>{STATE_LABEL[event.state]}</span>
              <span className={styles.time}>{event.formattedTime}</span>
            </div>
          ))
        )}
      </div>
    </Panel>
  );
}
