import { ALICE, expect, expectNoMockText, test } from './fixtures'

test('protected route sends a signed-out user to the brand page and back after Google callback', async ({
  page,
  baseURL,
}) => {
  // Google is external: answer the API's OAuth start with the callback the
  // backend would issue, carrying the redirect target through the round trip.
  await page.route('**/auth/google/login**', async (route) => {
    const redirect = new URL(route.request().url()).searchParams.get('redirect') ?? '/library'
    await route.fulfill({
      status: 302,
      headers: { Location: `${baseURL}/?${new URLSearchParams({ redirect })}#id_token=${ALICE}` },
    })
  })

  await page.goto('/tasks')
  await expect(page).toHaveURL(/\/\?redirect=(%2F|\/)tasks$/)
  await expect(page.getByRole('heading', { name: '开始使用视书' })).toBeVisible()

  // The agreement is enforced before leaving for Google.
  await page.getByRole('button', { name: /Google/ }).click()
  await expect(page.getByText('请先阅读并同意')).toBeVisible()

  await page.getByRole('checkbox').check()
  await page.getByRole('button', { name: /Google/ }).click()

  // First sign-in offers Colab onboarding and keeps the original destination.
  await expect(page).toHaveURL(/\/profile\/colab\?next=(%2F|\/)tasks$/)
  await page.getByRole('link', { name: '稍后连接，继续使用' }).click()
  await expect(page).toHaveURL(/\/tasks$/)
  expect(await page.evaluate(() => localStorage.getItem('v2md_token'))).toBe(ALICE)
  // The token fragment never stays in the address bar or history entry.
  expect(new URL(page.url()).hash).toBe('')
  await expect(page.getByText('Lesson 1').first()).toBeVisible()
  await expectNoMockText(page)
})

test('an expired credential returns to the brand page with a notice', async ({ page }) => {
  // Seed the stale token for the first document only, as a returning visitor would have it.
  await page.addInitScript(() => {
    if (sessionStorage.getItem('seeded')) return
    sessionStorage.setItem('seeded', '1')
    localStorage.setItem('v2md_token', 'expired-token')
  })
  await page.goto('/library')
  await expect(page).toHaveURL(/reason=expired/)
  await expect(page.getByText('登录已过期')).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('v2md_token'))).toBeNull()
})
