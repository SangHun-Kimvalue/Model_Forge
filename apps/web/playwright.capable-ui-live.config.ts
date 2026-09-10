import { mkdirSync } from "node:fs";
import path from "node:path";

import { defineConfig } from "@playwright/test";

/**
 * fallback ADR capable-model draft path — real chat UI reachability probe.
 *
 * Separate from every other config on purpose. This one wires the
 * **subscription-backed CLI provider** (`LLM_PROVIDER=cli`) into the running
 * product so the question "does a real chat input reach the capable-model
 * generator?" is answered by the product, not by a script that calls the
 * authoring pipeline directly.
 *
 * Opt-in via `MODEL_FORGE_RUN_CAPABLE_UI_LIVE=1`. Ports 8013/3013 are chosen so
 * this never collides with the Phase 12S route smoke (8012/3012) or the Orca
 * live suite (8002/3002).
 */

const LIVE_FLAG = "MODEL_FORGE_RUN_CAPABLE_UI_LIVE";
const isLive = process.env[LIVE_FLAG] === "1";

function optionalEnv(name: string, fallback: string): string {
  const value = process.env[name];
  return value && value.trim().length > 0 ? value : fallback;
}

function repoPath(value: string): string {
  return path.isAbsolute(value)
    ? value
    : path.resolve(path.resolve(process.cwd(), "../.."), value);
}

const runId = `${process.pid}-${Date.now()}`;

function backendEnv(): Record<string, string> {
  const draftQueueRoot = repoPath(
    optionalEnv(
      "ORCHESTRATOR_DRAFT_QUEUE_ROOT",
      `.model_forge/capable-ui-live-draft-queue-${runId}`,
    ),
  );
  mkdirSync(draftQueueRoot, { recursive: true });

  return {
    ORCHESTRATOR_ALLOWED_ORIGINS:
      "http://127.0.0.1:3013,http://localhost:3013",
    ORCHESTRATOR_LOG_LEVEL: optionalEnv("ORCHESTRATOR_LOG_LEVEL", "INFO"),
    ORCHESTRATOR_NATURAL_LANGUAGE_ROUTE_ENTRY: "true",
    CUBIFORGE_CAPABLE_DRAFT_ROUTE: "true",
    ORCHESTRATOR_DRAFT_QUEUE_ROOT: draftQueueRoot,

    // The capable generator is only assembled when a concrete non-mock
    // provider exists, and the provider is only constructed when some adapter
    // is prompt_based. Both are required for this probe to be meaningful:
    // without them the run would prove nothing (generator simply absent).
    LLM_PROVIDER: "cli",
    CLI_BACKEND: "claude",
    CLI_BIN: "claude",
    CLI_MODEL: "opus",
    CLI_TIMEOUT_S: optionalEnv("CLI_TIMEOUT_S", "300"),
    CAD_CODER_AGENT_ADAPTER: "prompt_based",
    CAD_CODER_DSL: "openscad",

    // Everything downstream stays mock: this probe is about which generator
    // authors the draft, not about CAD/slicer execution.
    PLANNER_AGENT_ADAPTER: "mock",
    CAD_MECHANICAL_ADAPTER: "mock",
    ORGANIC_GENERATOR_ADAPTER: "mock",
    VALIDATOR_ADAPTER: "mock",
    SLICER_ADAPTER: "mock",
  };
}

export default defineConfig({
  testDir: "./tests/live",
  testMatch: /capable-model-draft-ui\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 300_000,
  expect: { timeout: 60_000 },
  use: { baseURL: "http://127.0.0.1:3013", trace: "retain-on-failure" },
  webServer: isLive
    ? [
        {
          command:
            "python -m uvicorn apps.orchestrator.app:create_app --factory --host 127.0.0.1 --port 8013",
          cwd: "../..",
          url: "http://127.0.0.1:8013/docs",
          reuseExistingServer: false,
          timeout: 90_000,
          env: backendEnv(),
        },
        {
          command: "pnpm exec next dev -p 3013 -H 127.0.0.1",
          url: "http://127.0.0.1:3013",
          reuseExistingServer: false,
          timeout: 120_000,
          env: { NEXT_PUBLIC_ORCHESTRATOR_URL: "http://127.0.0.1:8013" },
        },
      ]
    : [],
});
