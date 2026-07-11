"use client";

import { useEffect } from "react";

import { ApprovalCard } from "@/components/ApprovalCard";
import { ArtifactList } from "@/components/ArtifactList";
import { ConversationPanel } from "@/components/ConversationPanel";
import { EventFeed } from "@/components/EventFeed";
import { MeshPreview } from "@/components/MeshPreview";
import { WorkStatusPanel } from "@/components/WorkStatusPanel";
import { connectEvents } from "@/lib/orchestrator";
import { useOrchestratorStore } from "@/stores/orchestrator-store";

import styles from "./workbench.module.css";

export default function WorkbenchPage(): JSX.Element {
  const sessionId = useOrchestratorStore((s) => s.sessionId);
  const client = useOrchestratorStore((s) => s.client);
  const ingestEvent = useOrchestratorStore((s) => s.ingestEvent);

  useEffect(() => {
    if (!sessionId) return;
    const handle = connectEvents(client.baseUrl, sessionId, {
      onEvent: ingestEvent,
    });
    return () => handle.close();
  }, [sessionId, client.baseUrl, ingestEvent]);

  return (
    <main className={styles.workbench} data-testid="workbench">
      <section className={styles.conversationCol}>
        <ApprovalCard />
        <ConversationPanel />
      </section>
      <aside className={styles.workCol}>
        <div className={styles.workTop}>
          <WorkStatusPanel />
        </div>
        <div className={styles.workMid}>
          <MeshPreview />
        </div>
        <div className={styles.workBottom}>
          <EventFeed />
        </div>
        <div className={styles.workArtifacts}>
          <ArtifactList />
        </div>
      </aside>
    </main>
  );
}
