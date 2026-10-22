import { API_BASE, ALICE, expect, expectNoMockText, signIn, test } from './fixtures'

test.beforeEach(async ({ page }) => {
  await signIn(page)
})

test('quick submit of an existing link reuses the queued run instead of duplicating it', async ({ page }) => {
  await page.goto('/tasks')
  const cards = page.getByRole('main').getByRole('heading', { level: 3 })
  await expect(cards.first()).toBeVisible()
  const before = await cards.count()

  const runs = page.waitForResponse((res) => res.url().endsWith('/runs') && res.request().method() === 'POST')
  await page.getByPlaceholder(/粘贴 B站/).fill('https://example.test/queued')
  await page.getByRole('button', { name: '开始解析' }).click()
  expect((await runs).status()).toBeLessThan(300)

  await expect(page.getByText('已加入转换队列')).toBeVisible()
  // Check right away, without retrying: the 3s poll would otherwise hide a
  // duplicate row that the store inserted optimistically.
  expect(await page.getByText('任务编号：queued').count()).toBe(1)
  expect(await cards.count()).toBe(before)
  await expect(page.getByPlaceholder(/粘贴 B站/)).toHaveValue('')
  await expect(cards).toHaveCount(before)
  await expectNoMockText(page)
})

test('import sheet validates input and keeps a server rejection visible', async ({ page }) => {
  await page.goto('/tasks?openImport=1')
  const sheet = page.getByRole('dialog', { name: '添加新的网课转换' })
  await expect(sheet.getByText('添加新的网课转换')).toBeVisible()
  // Only real, wired controls remain in the sheet.
  await expect(sheet.getByText('保存草稿')).toHaveCount(0)
  await expect(sheet.getByText('本地 MP4')).toHaveCount(0)

  await sheet.getByRole('button', { name: /立即开始转换排队/ }).click()
  await expect(sheet.getByRole('alert')).toHaveText('请输入视频链接')

  // Exercise the backend's real budget admission path (fixture toggle only).
  const enable = await fetch(`${API_BASE}/__tests/budget`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${ALICE}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: true }),
  })
  expect(enable.status).toBe(200)
  try {
    await sheet.getByPlaceholder(/粘贴 Bilibili/).fill('https://example.test/budget-rejected')
    await sheet.getByRole('button', { name: /立即开始转换排队/ }).click()
    await expect(sheet.getByRole('alert')).toContainText('提交失败')
    await expect(sheet).toBeVisible()
    await expect(sheet.getByPlaceholder(/粘贴 Bilibili/)).toHaveValue('https://example.test/budget-rejected')
  } finally {
    await fetch(`${API_BASE}/__tests/budget`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${ALICE}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: false }),
    })
  }
})
