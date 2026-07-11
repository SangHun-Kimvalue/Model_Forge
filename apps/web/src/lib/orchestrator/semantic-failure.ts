const FEATURE_LABELS: Record<string, string> = {
  bear_head: "곰 얼굴",
  ears: "귀",
  eyes: "눈",
  nose_or_muzzle: "코 또는 입 부분",
  body_or_paws: "몸통 또는 발",
  raised_logo_or_text: "양각 로고 또는 텍스트",
  motor_mounts: "모터 마운트",
  mounting_holes: "체결 구멍",
  central_hub: "중앙 허브",
  arms: "프레임 암",
  keyring_hole: "키링 구멍",
};

const CONSTRAINT_LABELS: Record<string, string> = {
  keyring_hole_must_be_cut_with_difference: "키링 구멍이 실제 관통 구멍으로 절삭되지 않았습니다.",
  raised_logo_or_text_geometry_missing: "양각 로고 또는 텍스트 형상이 확인되지 않았습니다.",
  geometry_blank_or_degenerate: "생성된 형상이 비어 있거나 출력 가능한 두께가 부족합니다.",
  geometry_has_no_printable_z_height: "출력 가능한 높이가 확인되지 않았습니다.",
  decorative_detail_height_too_low: "장식 형상의 높이가 너무 낮아 식별하기 어렵습니다.",
};

const CAD_CODER_ERROR_MESSAGES: Record<string, string> = {
  empty_response:
    "로컬 모델이 CAD 코드를 반환하지 않았습니다. 모델 실행 상태와 timeout을 확인해 주세요.",
  missing_main_call:
    "CAD 코드가 완성된 실행 진입점을 포함하지 못해 모델 생성을 중단했습니다.",
  openscad_geometry_assignment:
    "CAD 코드 생성 단계에서 형상 작성 규칙을 만족하지 못했습니다.",
  brace_unbalanced:
    "CAD 코드의 괄호 구조가 완성되지 않아 모델 생성을 중단했습니다.",
  syntax_contract_failed:
    "CAD 코드가 실행 가능한 형식 계약을 만족하지 못했습니다.",
  semantic_failure:
    "CAD 코드가 요청한 주요 형상을 충분히 반영하지 못했습니다.",
};

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function metadataFromManifest(payload: Record<string, unknown>): Record<string, unknown> {
  const manifest = payload.manifest;
  if (!manifest || typeof manifest !== "object") return {};
  const errors = (manifest as { errors?: unknown }).errors;
  if (!Array.isArray(errors) || errors.length === 0) return {};
  const firstError = errors[0];
  if (!firstError || typeof firstError !== "object") return {};
  const metadata = (firstError as { metadata?: unknown }).metadata;
  return metadata && typeof metadata === "object"
    ? (metadata as Record<string, unknown>)
    : {};
}

function stringValue(
  payload: Record<string, unknown>,
  metadata: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = payload[key] ?? metadata[key];
  return typeof value === "string" ? value : undefined;
}

function arrayValue(
  payload: Record<string, unknown>,
  metadata: Record<string, unknown>,
  key: string,
): string[] {
  const fromPayload = asStringArray(payload[key]);
  return fromPayload.length > 0 ? fromPayload : asStringArray(metadata[key]);
}

function labelFeature(feature: string): string {
  return FEATURE_LABELS[feature] ?? feature;
}

function labelConstraint(constraint: string): string {
  if (CONSTRAINT_LABELS[constraint]) return CONSTRAINT_LABELS[constraint];
  const holeCount = constraint.match(/^hole_count_below_(\d+)$/);
  if (holeCount) {
    return `요구한 체결 구멍 ${holeCount[1]}개가 확인되지 않았습니다.`;
  }
  const triangleCount = constraint.match(/^triangle_count_below_(\d+)$/);
  if (triangleCount) {
    return `형상 세부 묘사가 너무 단순합니다.`;
  }
  const vertexCount = constraint.match(/^vertex_count_below_(\d+)$/);
  if (vertexCount) {
    return `형상 세부 묘사가 너무 단순합니다.`;
  }
  const span = constraint.match(/^outer_span_xy_(?:out_of_tolerance|below|above)_(.+)$/);
  if (span) {
    return `요청한 외곽 크기 조건(${span[1]})을 만족하지 못했습니다.`;
  }
  return constraint;
}

function joinLabels(values: string[]): string {
  return values.join(", ");
}

function formatRetryHint(retryHint: string | undefined): string {
  if (!retryHint) {
    return "형상을 보강해 다시 생성해 주세요.";
  }
  return "재생성 힌트: 누락된 요구사항을 설명 문구가 아니라 실제 출력 가능한 형상으로 보강해 주세요.";
}

export function formatSubtaskFailureDetail(payload: Record<string, unknown>): string {
  const metadata = metadataFromManifest(payload);
  const stage = stringValue(payload, metadata, "stage");
  const errorType = stringValue(payload, metadata, "error_type") ?? "error";
  const subtaskId =
    typeof payload.subtask_id === "string" ? payload.subtask_id : "unknown";
  const missingFeatures = arrayValue(payload, metadata, "missing_features");
  const violatedConstraints = arrayValue(
    payload,
    metadata,
    "violated_constraints",
  );
  const retryHint = stringValue(payload, metadata, "retry_hint");
  const cadCoderErrorCode = stringValue(
    payload,
    metadata,
    "cad_coder_error_code",
  );
  const fallbackRecommended = stringValue(
    payload,
    metadata,
    "fallback_recommended",
  );
  const provider = stringValue(payload, metadata, "provider");
  const model = stringValue(payload, metadata, "model");
  const isCadCoderFailure =
    stage === "cad_coder" || cadCoderErrorCode !== undefined;
  const isSemanticFailure =
    stage === "semantic_validation" ||
    missingFeatures.length > 0 ||
    violatedConstraints.length > 0;

  if (isCadCoderFailure) {
    const sentences = [
      CAD_CODER_ERROR_MESSAGES[cadCoderErrorCode ?? ""] ??
        "CAD 코드 생성 단계에서 모델을 안전하게 실행할 수 없는 응답을 받았습니다.",
    ];
    if (cadCoderErrorCode === "openscad_geometry_assignment") {
      sentences.push(
        "같은 오류가 반복되면 자유 형상 생성 대신 안전한 템플릿 경로가 필요합니다.",
      );
    }
    if (cadCoderErrorCode === "empty_response") {
      const modelLabel = model ? ` (${provider ?? "provider"} / ${model})` : "";
      sentences.push(`이 실패는 CAD 품질이 아니라 로컬 모델 응답 안정성 문제입니다${modelLabel}.`);
    }
    if (fallbackRecommended === "template") {
      sentences.push("안전한 템플릿 기반 생성으로 전환하는 것이 좋습니다.");
    }
    return sentences.join(" ");
  }

  if (!isSemanticFailure) {
    return `${subtaskId} / ${errorType}`;
  }

  const sentences = ["모델 생성이 중단되었습니다."];
  if (missingFeatures.length > 0) {
    sentences.push(
      `요청한 형상에서 ${joinLabels(
        missingFeatures.map(labelFeature),
      )}을 확인하지 못했습니다.`,
    );
  }
  if (violatedConstraints.length > 0) {
    sentences.push(violatedConstraints.map(labelConstraint).join(" "));
  }
  sentences.push(formatRetryHint(retryHint));
  return sentences.join(" ");
}
