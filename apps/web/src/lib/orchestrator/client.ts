/**
 * HTTP client for the Phase 6 orchestrator API.
 *
 * `NEXT_PUBLIC_ORCHESTRATOR_URL` is read at module load so a single
 * client targets one backend per page; tests inject a base URL via
 * `createOrchestratorClient`.
 */

import type {
  ApproveRequest,
  ApproveResponse,
  ChatRequest,
  ChatResponse,
  ClarifyRequest,
  ClarifyResponse,
  IntakeChoiceRequest,
  IntakeChoiceResponse,
  IntakeNextStepRequest,
  IntakeNextStepView,
  RuntimeAssetExecutionRequest,
  RuntimeAssetExecutionResponse,
  SessionCreateRequest,
  SessionCreateResponse,
  SessionStateView,
} from "./schemas";

/** Wire shape sent to `POST /approve/{gate_id}` — gate_id is in the path. */
export interface ApproveJobBody {
  session_id: string;
  decision: ApproveRequest["decision"];
  comments?: string | null;
}

export class OrchestratorRequestError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`Orchestrator ${status}: ${detail}`);
    this.name = "OrchestratorRequestError";
  }
}

export interface OrchestratorClient {
  readonly baseUrl: string;
  createSession(body?: SessionCreateRequest): Promise<SessionCreateResponse>;
  getSession(sessionId: string): Promise<SessionStateView>;
  sendChat(body: ChatRequest): Promise<ChatResponse>;
  clarify(body: ClarifyRequest): Promise<ClarifyResponse>;
  chooseIntakeAction(body: IntakeChoiceRequest): Promise<IntakeChoiceResponse>;
  advanceIntakeNextStep(body: IntakeNextStepRequest): Promise<IntakeNextStepView>;
  executeRuntimeAsset(
    body: RuntimeAssetExecutionRequest,
  ): Promise<RuntimeAssetExecutionResponse>;
  approveGate(body: ApproveRequest): Promise<ApproveResponse>;
}

async function parseJsonOrThrow<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* ignore body parse errors; keep statusText */
    }
    throw new OrchestratorRequestError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function createOrchestratorClient(
  baseUrl: string = process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ??
    "http://localhost:8000",
): OrchestratorClient {
  const trimmed = baseUrl.replace(/\/+$/, "");
  const headers: Record<string, string> = { "Content-Type": "application/json" };

  return {
    baseUrl: trimmed,
    async createSession(body: SessionCreateRequest = {}) {
      const response = await fetch(`${trimmed}/session`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      return parseJsonOrThrow<SessionCreateResponse>(response);
    },
    async getSession(sessionId: string) {
      const response = await fetch(`${trimmed}/session/${sessionId}`);
      return parseJsonOrThrow<SessionStateView>(response);
    },
    async sendChat(body: ChatRequest) {
      const response = await fetch(`${trimmed}/chat`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      return parseJsonOrThrow<ChatResponse>(response);
    },
    async clarify(body: ClarifyRequest) {
      const response = await fetch(`${trimmed}/clarify`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      return parseJsonOrThrow<ClarifyResponse>(response);
    },
    async chooseIntakeAction(body: IntakeChoiceRequest) {
      const response = await fetch(`${trimmed}/intake-choice`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      return parseJsonOrThrow<IntakeChoiceResponse>(response);
    },
    async advanceIntakeNextStep(body: IntakeNextStepRequest) {
      const response = await fetch(`${trimmed}/intake-next-step`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      return parseJsonOrThrow<IntakeNextStepView>(response);
    },
    async executeRuntimeAsset(body: RuntimeAssetExecutionRequest) {
      const response = await fetch(`${trimmed}/runtime-asset-execution`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      return parseJsonOrThrow<RuntimeAssetExecutionResponse>(response);
    },
    async approveGate(body: ApproveRequest) {
      // Prefer DESIGN.md long-form contract: gate_id in URL path.
      // Backend `ApproveJobRequest` uses `extra="forbid"`, so strip gate_id
      // from the body before POSTing.
      const { gate_id, ...rest } = body;
      const jobBody: ApproveJobBody = rest;
      const response = await fetch(
        `${trimmed}/approve/${encodeURIComponent(gate_id)}`,
        {
          method: "POST",
          headers,
          body: JSON.stringify(jobBody),
        },
      );
      return parseJsonOrThrow<ApproveResponse>(response);
    },
  };
}
