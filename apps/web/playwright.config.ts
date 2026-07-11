import { defineConfig } from "@playwright/test";

/**
 * Phase 7 workbench E2E.
 *
 * Two webServers spin up in parallel:
 *  - uvicorn FastAPI orchestrator on :8000 (mock planner + mock adapters)
 *  - Next.js dev server on :3000
 *
 * Both must be reachable before the spec runs. `reuseExistingServer`
 * lets developers point Playwright at long-running dev servers; CI
 * starts fresh processes.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "on-first-retry",
  },
  webServer: [
    {
      command:
        "python -m uvicorn apps.orchestrator.app:create_app --factory --host 127.0.0.1 --port 8000",
      cwd: "../..",
      url: "http://127.0.0.1:8000/docs",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        ORCHESTRATOR_ALLOWED_ORIGINS: "http://127.0.0.1:3000,http://localhost:3000",
      },
    },
    {
      command: "pnpm exec next dev -p 3000 -H 127.0.0.1",
      url: "http://127.0.0.1:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        NEXT_PUBLIC_ORCHESTRATOR_URL: "http://127.0.0.1:8000",
      },
    },
  ],
});
