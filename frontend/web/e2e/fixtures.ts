import { test as base, expect, type Page } from '@playwright/test'
import { API_BASE } from '../playwright.config'

export const ALICE = 'integration-alice'

/** Text that only a prototype/mock build would show to a user. */
export const MOCK_TEXT = /mock|模拟支付|草稿已保存|u_mock/i

export async function resetBackend(): Promise<void> {
  const res = await fetch(`${API_BASE}/__tests/reset`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${ALICE}` },
  })
  expect(res.status).toBe(200)
}

export async function signIn(page: Page): Promise<void> {
  await page.addInitScript((token) => localStorage.setItem('v2md_token', token), ALICE)
}

export async function expectNoMockText(page: Page): Promise<void> {
  await expect(page.locator('body')).not.toContainText(MOCK_TEXT)
}

export const test = base.extend<{ backend: void }>({
  backend: [
    // eslint-disable-next-line no-empty-pattern -- Playwright fixtures must destructure their dependencies.
    async ({}, use) => {
      await resetBackend()
      await use()
    },
    { auto: true },
  ],
})

export { expect, API_BASE }
