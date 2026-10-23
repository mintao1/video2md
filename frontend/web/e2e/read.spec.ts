import { expect, expectNoMockText, signIn, test } from './fixtures'

test('a finished run opens from the library and renders its note with authenticated media', async ({ page }) => {
  await signIn(page)
  await page.goto('/library')
  const card = page.locator('article').filter({ hasText: '开始精读' }).first()
  await expect(card).toBeVisible()
  await card.getByRole('button', { name: /开始精读/ }).click()

  await expect(page).toHaveURL(/\/read\/finished/)
  const note = page.locator('article.prose-v2md')
  await expect(note.getByRole('heading', { name: 'Integration lesson' })).toBeVisible()

  // Media is fetched with the Bearer token and shown through a blob: URL only.
  const image = note.locator('img[alt="demo.png"]')
  await expect(image).toHaveAttribute('src', /^blob:/)
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0)
  await expectNoMockText(page)

  // Reading progress is written back and survives a reload.
  await page.reload()
  await expect(note.getByRole('heading', { name: 'Integration lesson' })).toBeVisible()
  await page.goto('/library')
  await expect(page.getByText('最近在读 · 进度 100%')).toBeVisible()
})

test('library offers no download where the backend has no bundle (vault mode)', async ({ page }) => {
  await signIn(page)
  await page.goto('/library')
  await expect(page.getByRole('button', { name: /开始精读|继续阅读/ }).first()).toBeVisible()
  // The fixture runs in vault mode: notes are written to the vault and
  // /runs/{id}/bundle answers 409, so a download button would be a fake action.
  await expect(page.getByRole('button', { name: '下载 Markdown 原始包' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /分享/ })).toHaveCount(0)
  await expect(page.getByText('立即同步')).toHaveCount(0)
})
