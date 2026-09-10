/**
 * Mirror of `apps/orchestrator/schemas.py` (Phase 6).
 *
 * Why hand-written and not codegen'd:
 *  - Phase 7 keeps tooling minimal (P10). Adding an OpenAPI codegen
 *    step would pull a build-time dependency on the FastAPI process.
 *  - The orchestrator surface is small and the test suite already
 *    pins the wire format on the Python side. Drift will surface as
 *    Vitest store failures and Playwright E2E failures, both of
 *    which run against the live FastAPI server.
 *
 * If this file diverges from `apps/orchestrator/schemas.py`, treat the
 * Python schemas as the source of truth and update here.
 */

export type SessionState =
  | "created"
  | "planning"
  | "awaiting_user_input"
  | "awaiting_approval"
  | "running"
  | "completed"
  | "failed";

export type ApprovalDecision = "approved" | "rejected";

export type ApprovalStatus = "pending" | "approved" | "rejected";

export type ClarificationStatus =
  | "pending"
  | "answered"
  | "cancelled"
  | "expired";

export type ClarificationAnswerType = "text" | "number" | "choice";

export type IntakeChoiceAction =
  | "use_existing_runtime_asset"
  | "review_existing_draft"
  | "adapt_similar_candidate"
  | "create_new_draft"
  | "ask_for_more_info";

export type IntakeNextStepStatus =
  | "runtime_asset_selected"
  | "draft_review_handoff"
  | "similar_candidate_review"
  | "draft_authoring_started"
  | "information_needed";

export type SessionEventKind =
  | "state_changed"
  | "natural_language_route_selected"
  | "intake_choice_recorded"
  | "intake_next_step_recorded"
  | "planning_completed"
  | "subtask_started"
  | "subtask_progress"
  | "subtask_completed"
  | "subtask_failed"
  | "approval_requested"
  | "approval_resolved"
  | "clarification_requested"
  | "clarification_resolved"
  | "error";

export interface SessionCreateRequest {
  client_label?: string | null;
}

export interface SessionCreateResponse {
  session_id: string;
  trace_id: string;
  state: SessionState;
  created_at: string;
}

export interface ChatRequest {
  session_id: string;
  message: string;
}

export interface SubtaskView {
  id: string;
  kind: "mechanical" | "organic";
  description: string;
  depends_on: readonly string[];
}

export interface LLMAssistShadowView {
  mode: string;
  succeeded: boolean;
  used_for_decision: boolean;
  model_name: string;
  provider_name: string;
  prompt_hash: string;
  schema_version: string;
  deterministic_route: string;
  confidence?: number | null;
  fallback_reason?: string | null;
  candidate_route?: string | null;
  candidate_subjects: readonly string[];
  candidate_style?: string | null;
  clarification_question?: string | null;
  requirement_candidate?: Record<string, unknown> | null;
  search_expansion?: Record<string, unknown> | null;
  raw_response_excerpt?: string | null;
}

export interface NaturalLanguageRouteView {
  selected_route: string;
  route_snapshot_id: string;
  reason: string;
  request_id?: string | null;
  runtime_asset_id?: string | null;
  draft_asset_id?: string | null;
  candidate_asset_ids: readonly string[];
  clarification_required: boolean;
  clarification_questions: readonly string[];
  generation_allowed: boolean;
  requirement: Record<string, unknown>;
  intake_decision?: AssetIntakeDecisionView | null;
  shadow_result?: LLMAssistShadowView | null;
}

export interface AssetIntakeDecisionView {
  status: string;
  reason: string;
  runtime_asset_id?: string | null;
  draft_asset_id?: string | null;
  candidate_asset_ids: readonly string[];
  reference_candidate_ids: readonly string[];
  new_draft_allowed: boolean;
  metadata: Record<string, unknown>;
}

export interface PendingGateView {
  gate_id: string;
  prompt: string;
  requested_at: string;
  status: ApprovalStatus;
}

export interface ClarificationQuestion {
  id: string;
  field: string;
  question: string;
  required: boolean;
  answer_type: ClarificationAnswerType;
  choices: readonly string[];
  unit?: string | null;
}

export interface PendingClarificationView {
  clarification_id: string;
  session_id: string;
  subtask_id?: string | null;
  trace_id?: string | null;
  reason: string;
  questions: readonly ClarificationQuestion[];
  source_decision: Record<string, unknown>;
  plan_snapshot_id?: string | null;
  requirement_snapshot_id?: string | null;
  requested_at: string;
  status: ClarificationStatus;
}

/**
 * Mirror of the backend IP/abuse pre-filter verdict (fallback ADR item 6).
 *
 * Typed with its four named fields rather than `Record<string, unknown>`
 * because a consumer reads `user_message_ko` to decide what the user is told;
 * an index signature lets a typo compile and surface only as a blank refusal
 * at runtime.
 */
export interface PrefilterDecisionView {
  decision: string;
  matched_rule_id: string | null;
  reason_code: string;
  user_message_ko: string;
}

export interface ChatResponse {
  session_id: string;
  trace_id: string;
  state: SessionState;
  subtasks: readonly SubtaskView[];
  clarifying_questions: readonly string[];
  pending_gate: PendingGateView | null;
  pending_clarification?: PendingClarificationView | null;
  route_result?: NaturalLanguageRouteView | null;
  /**
   * Present only when the request was refused before the planner ran. Every
   * planner-output field is empty in that reply, so a client that ignores this
   * field reports a safety refusal as a planner failure.
   */
  prefilter?: PrefilterDecisionView | null;
}

export interface ApproveRequest {
  session_id: string;
  gate_id: string;
  decision: ApprovalDecision;
  comments?: string | null;
}

/**
 * Mirror of `ApproveJobRequest` (Python). Body for `POST /approve/{job_id}`.
 * `gate_id` is carried in the URL path — backend uses `extra="forbid"` so
 * including it in the body is a 422.
 */
export interface ApproveJobRequest {
  session_id: string;
  decision: ApprovalDecision;
  comments?: string | null;
}

export interface ApproveResponse {
  session_id: string;
  gate_id: string;
  state: SessionState;
  decision: ApprovalDecision;
}

export type ClarificationAnswerValue = string | number;

export interface ClarifyRequest {
  session_id: string;
  clarification_id: string;
  answers: Record<string, ClarificationAnswerValue>;
}

export interface ClarifyResponse {
  session_id: string;
  clarification_id: string;
  accepted: boolean;
  state: SessionState;
  route_result?: NaturalLanguageRouteView | null;
  pending_clarification?: PendingClarificationView | null;
}

export interface IntakeChoiceRequest {
  session_id: string;
  action: IntakeChoiceAction;
  route_snapshot_id: string;
}

export interface IntakeChoiceResponse {
  session_id: string;
  accepted: boolean;
  action: IntakeChoiceAction;
  state: SessionState;
  route_snapshot_id: string;
  intake_status?: string | null;
  selected_route?: string | null;
  runtime_asset_id?: string | null;
  draft_asset_id?: string | null;
  candidate_asset_ids: readonly string[];
  reference_candidate_ids: readonly string[];
  draft_authoring_requested: boolean;
  runtime_execution_started: boolean;
  runtime_catalog_registered: boolean;
  product_ready: boolean;
  release_allowed: boolean;
  message: string;
}

export interface IntakeNextStepRequest {
  session_id: string;
  route_snapshot_id: string;
}

export interface IntakeNextStepView {
  session_id: string;
  action: IntakeChoiceAction;
  status: IntakeNextStepStatus;
  state: SessionState;
  route_snapshot_id: string;
  selected_route?: string | null;
  runtime_asset_id?: string | null;
  draft_asset_id?: string | null;
  candidate_asset_ids: readonly string[];
  reference_candidate_ids: readonly string[];
  draft_asset?: Record<string, unknown> | null;
  draft_authoring_status?: string | null;
  draft_authoring_reason?: string | null;
  // fallback ADR item 6: structured IP/abuse pre-filter decision when the capable
  // model was blocked before being invoked (optional additive field).
  prefilter?: Record<string, unknown> | null;
  review_required: boolean;
  runtime_execution_started: boolean;
  runtime_catalog_registered: boolean;
  product_ready: boolean;
  release_allowed: boolean;
  message: string;
}

export interface RuntimeAssetExecutionRequest {
  session_id: string;
  route_snapshot_id: string;
}

export interface RuntimeAssetExecutionResponse {
  session_id: string;
  state: SessionState;
  route_snapshot_id: string;
  runtime_asset_id: string;
  subtask_id: string;
  runtime_execution_started: boolean;
  runtime_catalog_registered: boolean;
  product_ready: boolean;
  release_allowed: boolean;
  next_step: IntakeNextStepView;
  manifest: SubtaskArtifactManifest;
  message: string;
}

export interface SessionEvent {
  session_id: string;
  trace_id: string;
  kind: SessionEventKind;
  state: SessionState;
  occurred_at: string;
  payload: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Artifact manifest (manifest ADR). Mirrors `modules.artifacts.schemas`.
// Only `subtask_completed` (and `subtask_failed`) events carry a manifest.
// ---------------------------------------------------------------------------

export type ArtifactKind =
  | "mechanical_mesh"
  | "organic_mesh"
  | "merged_mesh"
  | "gcode"
  | "threemf"
  | "validation_report"
  | "preview_mesh"
  | "preview_image";

export type ArtifactStatus = "ok" | "partial" | "quarantined";

export type ManifestStatus = "success" | "failed" | "partial" | "quarantined";

export interface ManifestError {
  stage: string;
  error_type: string;
  detail: string;
  metadata?: Record<string, unknown>;
}

export interface ArtifactRef {
  kind: ArtifactKind;
  relative_uri: string;
  mime: string;
  size_bytes: number;
  sha256: string;
  producer_adapter: string;
  producer_version: string;
  created_at: string;
  status: ArtifactStatus;
  metadata?: Record<string, unknown>;
}

export interface SubtaskArtifactManifest {
  schema_version: string;
  session_id: string;
  subtask_id: string;
  created_at: string;
  artifacts: readonly ArtifactRef[];
  status: ManifestStatus;
  errors: readonly ManifestError[];
  plan_snapshot_id?: string | null;
  approval_gate_id?: string | null;
  trace_id?: string | null;
}

export interface SubtaskCompletedPayload {
  subtask_id: string;
  kind: string;
  manifest?: SubtaskArtifactManifest;
}

export interface SessionStateView {
  session_id: string;
  trace_id: string;
  state: SessionState;
  created_at: string;
  pending_gates: readonly PendingGateView[];
  pending_clarification?: PendingClarificationView | null;
  subtasks: readonly SubtaskView[];
  latest_route_result?: NaturalLanguageRouteView | null;
  latest_intake_choice?: IntakeChoiceResponse | null;
  latest_intake_next_step?: IntakeNextStepView | null;
}

export const TERMINAL_STATES: ReadonlySet<SessionState> = new Set<SessionState>([
  "completed",
  "failed",
]);

/**
 * Phase 6 contract: `/chat` is only accepted in the `created` state.
 * Once the planner starts (`planning`), is awaiting approval, running,
 * or terminal, a new `/chat` would be rejected server-side (409) or
 * would overwrite the active plan. The UI mirrors this exactly so the
 * user can never type into a disallowed state.
 *
 * Note: `null` (no session yet) is also locked here; the store selector
 * surfaces "create a session first" separately.
 */
export function isChatLockedState(state: SessionState | null): boolean {
  return state !== "created";
}
