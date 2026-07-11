"use client";

import { useState } from "react";

import { Panel } from "./Panel";
import styles from "./ConversationPanel.module.css";
import {
  selectChatDisabledReason,
  selectIsChatDisabled,
  useOrchestratorStore,
} from "@/stores/orchestrator-store";

export function ConversationPanel(): JSX.Element {
  const sessionId = useOrchestratorStore((s) => s.sessionId);
  const creatingSession = useOrchestratorStore((s) => s.creatingSession);
  const messages = useOrchestratorStore((s) => s.messages);
  const sendChat = useOrchestratorStore((s) => s.sendChat);
  const answerClarification = useOrchestratorStore((s) => s.answerClarification);
  const createSession = useOrchestratorStore((s) => s.createSession);
  const pendingClarification = useOrchestratorStore((s) => s.pendingClarification);
  const sendingClarification = useOrchestratorStore((s) => s.sendingClarification);
  const isChatDisabled = useOrchestratorStore(selectIsChatDisabled);
  const chatDisabledReason = useOrchestratorStore(selectChatDisabledReason);
  const lastError = useOrchestratorStore((s) => s.lastError);

  const [draft, setDraft] = useState("");
  const isClarificationMode = Boolean(pendingClarification);
  const inputDisabled = isClarificationMode
    ? !sessionId || creatingSession || sendingClarification
    : isChatDisabled;
  const disabledReason = isClarificationMode
    ? sendingClarification
      ? "답변을 보내는 중입니다…"
      : "추가 정보에 답하면 작업을 이어갑니다."
    : chatDisabledReason;

  const handleSubmit = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    if (inputDisabled) return;
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    try {
      if (pendingClarification) {
        await answerClarification(text);
      } else {
        await sendChat(text);
      }
    } catch (err) {
      // Error stored in store.lastError and displayed below the composer.
      console.error("[ConversationPanel] sendChat failed:", err);
    }
  };

  const handleNewSession = async (): Promise<void> => {
    try {
      await createSession();
    } catch (err) {
      console.error("[ConversationPanel] createSession failed:", err);
    }
  };

  return (
    <Panel
      title="대화"
      testId="conversation-panel"
      actions={
        <button
          type="button"
          className="primary"
          onClick={handleNewSession}
          disabled={creatingSession}
          data-testid="new-session-button"
        >
          {sessionId ? "새 세션" : "세션 시작"}
        </button>
      }
    >
      <div className={styles.transcript} data-testid="conversation-transcript">
        {messages.length === 0 ? (
          <p className={styles.empty}>
            {sessionId
              ? "출력할 부품을 설명하면 작업 계획을 만듭니다."
              : "세션을 시작하면 작업 대화를 진행할 수 있습니다."}
          </p>
        ) : (
          messages.map((message) => (
            <div
              key={message.id}
              className={`${styles.message} ${styles[`role_${message.role}`]}`}
            >
              <span className={styles.role}>
                {message.role === "user"
                  ? "사용자"
                  : message.role === "assistant"
                    ? "어시스턴트"
                    : "시스템"}
              </span>
              <span className={styles.text}>{message.text}</span>
            </div>
          ))
        )}
      </div>
      <form className={styles.composer} onSubmit={handleSubmit}>
        <textarea
          rows={3}
          placeholder={
            pendingClarification
              ? "질문에 대한 답변을 입력해주세요…"
              : "출력할 부품을 설명해주세요…"
          }
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={inputDisabled}
          data-testid="chat-input"
        />
        <div className={styles.composerRow}>
          <span className={styles.hint} data-testid="chat-disabled-reason">
            {disabledReason ?? "\u00A0"}
          </span>
          <button
            type="submit"
            className="primary"
            disabled={inputDisabled || draft.trim().length === 0}
            data-testid="chat-submit"
          >
            보내기
          </button>
        </div>
        {lastError ? (
          <div className={styles.error} data-testid="chat-error">
            {lastError}
          </div>
        ) : null}
      </form>
    </Panel>
  );
}
