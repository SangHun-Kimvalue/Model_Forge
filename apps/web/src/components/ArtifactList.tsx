"use client";

/**
 * Phase 9E — manifest-backed artifact list.
 *
 * Renders every artifact accepted into the store by `ingestEvent`
 * (ADR-0005 `manifest.artifacts[]` with `status === "ok"`).
 *
 * Why a dedicated panel:
 *  - The 3D preview only shows the single preferred mesh, so users
 *    couldn't see produced G-code / 3MF / validation reports without
 *    inspecting the network tab.
 *  - The download link is the only sanctioned way to fetch the
 *    artifact — the `/artifacts/...` route enforces the manifest
 *    allowlist on the backend, so this list never offers raw paths.
 */

import { Panel } from "./Panel";
import styles from "./ArtifactList.module.css";
import {
  useOrchestratorStore,
  type ArtifactEntry,
} from "@/stores/orchestrator-store";
import type { ArtifactKind } from "@/lib/orchestrator";

const KIND_LABEL: Record<ArtifactKind, string> = {
  mechanical_mesh: "기구 메시",
  organic_mesh: "유기 메시",
  merged_mesh: "병합 메시",
  gcode: "G-code",
  threemf: "3MF",
  validation_report: "검증 리포트",
  preview_mesh: "프리뷰 메시",
  preview_image: "프리뷰 이미지",
};

export function ArtifactList(): JSX.Element {
  const artifacts = useOrchestratorStore((s) => s.artifacts);
  return (
    <Panel title="생성 파일" testId="artifact-list">
      {artifacts.length === 0 ? (
        <p className={styles.empty} data-testid="artifact-list-empty">
          완료 후 STL, 3MF, G-code가 표시됩니다.
        </p>
      ) : (
        <ul className={styles.list}>
          {artifacts.map((artifact) => (
            <ArtifactRow key={artifact.id} artifact={artifact} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function ArtifactRow({ artifact }: { artifact: ArtifactEntry }): JSX.Element {
  const visualMessage = visualQualityMessage(artifact.metadata);
  return (
    <li className={styles.row} data-testid="artifact-row">
      <div className={styles.main}>
        <span
          className={styles.kind}
          data-testid="artifact-kind"
          data-artifact-kind={artifact.kind}
        >
          {KIND_LABEL[artifact.kind] ?? artifact.kind}
        </span>
        <a
          href={artifact.url}
          className={styles.link}
          data-testid="artifact-download"
          download={artifact.label}
          target="_blank"
          rel="noreferrer noopener"
        >
          {artifact.label}
        </a>
      </div>
      {visualMessage ? (
        <span className={styles.review} data-testid="visual-quality-status">
          {visualMessage}
        </span>
      ) : null}
    </li>
  );
}

function visualQualityMessage(metadata: Record<string, unknown> | undefined): string {
  if (!metadata || metadata.visual_quality_required !== true) return "";
  const status =
    typeof metadata.visual_quality_status === "string"
      ? metadata.visual_quality_status
      : "review_required";
  if (status === "manual_pass" || status === "automated_pass") {
    return "시각 품질 검토를 통과했습니다.";
  }
  if (status === "manual_pass_candidate") {
    return "시각 품질 후보로 보이지만 최종 검토 전입니다.";
  }
  if (status === "manual_fail" || status === "automated_fail") {
    return "시각 품질 검토를 통과하지 못했습니다.";
  }
  return "모델 파일은 생성되었습니다. 다만 요청한 형상처럼 보이는지 시각 품질 검토가 필요합니다.";
}
