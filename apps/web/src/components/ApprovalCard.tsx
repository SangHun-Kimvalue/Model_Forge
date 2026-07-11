"use client";

import { Panel } from "./Panel";
import styles from "./ApprovalCard.module.css";
import { useOrchestratorStore } from "@/stores/orchestrator-store";

export function ApprovalCard(): JSX.Element | null {
  const pendingGate = useOrchestratorStore((s) => s.pendingGate);
  const pendingApproval = useOrchestratorStore((s) => s.pendingApproval);
  const subtaskCount = useOrchestratorStore((s) => s.subtasks.length);
  const approveGate = useOrchestratorStore((s) => s.approveGate);

  if (!pendingGate || pendingGate.status !== "pending") return null;

  const buttonsDisabled = pendingApproval || pendingGate.status !== "pending";

  const handle = (decision: "approved" | "rejected") => async (): Promise<void> => {
    try {
      await approveGate(decision);
    } catch (err) {
      // Error stored in store.lastError and surfaced in the UI.
      console.error("[ApprovalCard] approveGate failed:", err);
    }
  };

  return (
    <Panel title="실행 확인" testId="approval-card">
      <p className={styles.prompt} data-testid="approval-prompt">
        계획된 하위 작업 {subtaskCount}개를 실행할까요?
      </p>
      <div className={styles.footer}>
        <div className={styles.actions}>
          <button
            type="button"
            className="primary"
            onClick={handle("approved")}
            disabled={buttonsDisabled}
            data-testid="approve-button"
          >
            {pendingApproval ? "…" : "실행"}
          </button>
          <button
            type="button"
            className="danger"
            onClick={handle("rejected")}
            disabled={buttonsDisabled}
            data-testid="reject-button"
          >
            {pendingApproval ? "…" : "취소"}
          </button>
        </div>
        <p className={styles.status} data-testid="approval-status">
          상태: {pendingGate.status === "pending"
            ? "대기"
            : pendingGate.status === "approved"
              ? "실행됨"
              : "취소됨"}
        </p>
      </div>
    </Panel>
  );
}
