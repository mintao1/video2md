import { defineConfig, devices } from '@playwright/test'
import { fileURLToPath } from 'node:url'

// Real FastAPI fixture (backend/tests/fixtures/web_integration_api.py) + Vite dev
// server. Only external identity verification is replaced; no video/GPU/LLM work runs.
// The config is evaluated in the runner and again in each worker; workers inherit
// the runner's environment, so a port chosen once here is shared by both.
process.env.V2MD_E2E_API_PORT ||= String(20000 + Math.floor(Math.random() * 20000))
export const API_PORT = Number(process.env.V2MD_E2E_API_PORT)
export const API_BASE = `http://127.0.0.1:${API_PORT}`
const WEB_PORT = 5173
const repoRoot = fileURLToPath(new URL('../..', import.meta.url))
const python = process.env.V2MD_TEST_PYTHON || `${repoRoot}/backend/.venv/bin/python`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1, // One shared backend fixture whose state is reset before each test.
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    ...devices['Pixel 7'],
    trace: 'retain-on-failure',
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
  projects: [
    { name: 'e2e', testIgnore: /screenshots\.spec\.ts/ },
    { name: 'screenshots', testMatch: /screenshots\.spec\.ts/ },
  ],
  webServer: [
    {
      command: `sh -c 'exec "${python}" backend/tests/fixtures/web_integration_api.py "$(mktemp -d)"'`,
      cwd: repoRoot,
      env: { V2MD_TEST_PORT: String(API_PORT) },
      url: `${API_BASE}/healthz`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      env: { VITE_API_BASE_URL: API_BASE },
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
})
