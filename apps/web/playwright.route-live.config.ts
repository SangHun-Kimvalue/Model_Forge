import { defineConfig } from "@playwright/test";

/**
 * Phase 12S natural-language route UI smoke.
 *
 * This config is intentionally separate from the default Phase 7 e2e config:
 * route entry is opt-in and must not intercept ordinary planner/pipeline tests.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: /natural-language-route\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  use: {
    baseURL: "http://127.0.0.1:3012",
    trace: "on-first-retry",
  },
  webServer: [
    {
      command:
        "python -m uvicorn apps.orchestrator.app:create_app --factory --host 127.0.0.1 --port 8012",
      cwd: "../..",
      url: "http://127.0.0.1:8012/docs",
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ORCHESTRATOR_ALLOWED_ORIGINS:
          "http://127.0.0.1:3012,http://localhost:3012",
        ORCHESTRATOR_NATURAL_LANGUAGE_ROUTE_ENTRY: "true",
      },
    },
    {
      command: "pnpm exec next dev -p 3012 -H 127.0.0.1",
      url: "http://127.0.0.1:3012",
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        NEXT_PUBLIC_ORCHESTRATOR_URL: "http://127.0.0.1:8012",
      },
    },
  ],
});
