import type { NaturalLanguageRouteView } from "./schemas";

export type CandidateExplanationSource =
  | "runtime_catalog"
  | "draft_queue"
  | "reference_candidate"
  | "new_draft"
  | "ask_user";

export interface CandidateExplanationView {
  candidate_id: string;
  asset_id?: string | null;
  source: CandidateExplanationSource;
  match_level: string;
  reason_code: string;
  display_text_ko: string;
  subject?: string | null;
  category?: string | null;
  style?: string | null;
}

const SOURCE_LABELS: Record<CandidateExplanationSource, string> = {
  runtime_catalog: "검증된 후보",
  draft_queue: "기존 초안",
  reference_candidate: "유사 후보",
  new_draft: "새 초안 경계",
  ask_user: "추가 정보 필요",
};

const KNOWN_SOURCES = new Set<CandidateExplanationSource>(
  Object.keys(SOURCE_LABELS) as CandidateExplanationSource[],
);

export function candidateSourceLabel(source: CandidateExplanationSource): string {
  return SOURCE_LABELS[source];
}

export function candidateExplanationsFromRoute(
  route: NaturalLanguageRouteView | null,
): CandidateExplanationView[] {
  return candidateExplanationsFromIntakeDecision(route?.intake_decision ?? null);
}

export function candidateExplanationsFromIntakeDecision(
  intakeDecision: unknown,
): CandidateExplanationView[] {
  const intake = objectRecord(intakeDecision);
  const metadata = objectRecord(intake?.metadata);
  const value = metadata?.candidate_explanations;
  if (!Array.isArray(value)) return [];
  return value
    .map((row) => parseCandidateExplanation(row))
    .filter((row): row is CandidateExplanationView => row !== null);
}

function parseCandidateExplanation(value: unknown): CandidateExplanationView | null {
  const row = objectRecord(value);
  if (!row) return null;

  const displayText = nonEmptyString(row.display_text_ko);
  const source = knownSource(row.source);
  if (!displayText || !source) return null;

  return {
    candidate_id: nonEmptyString(row.candidate_id) ?? "",
    asset_id: nullableString(row.asset_id),
    source,
    match_level: nonEmptyString(row.match_level) ?? "",
    reason_code: nonEmptyString(row.reason_code) ?? "",
    display_text_ko: displayText,
    subject: nullableString(row.subject),
    category: nullableString(row.category),
    style: nullableString(row.style),
  };
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function nullableString(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return value;
  return typeof value === "string" ? value : undefined;
}

function knownSource(value: unknown): CandidateExplanationSource | null {
  if (typeof value !== "string") return null;
  return KNOWN_SOURCES.has(value as CandidateExplanationSource)
    ? (value as CandidateExplanationSource)
    : null;
}
