import { test, expect } from '@playwright/test'

const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
)

async function installApiFixture(page, { notes = 'ok', media = 'ok' } = {}) {
  await page.context().addInitScript(() => {
    window.localStorage.setItem('v2pdf_token', 'pdf-e2e-token')
    const calls = () => Number(window.__v2pdfPrintCalls || 0)
    window.print = () => { window.__v2pdfPrintCalls = calls() + 1 }
  })

  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL('http://127.0.0.1:4173').origin) {
      await route.continue()
      return
    }

    if (url.pathname === '/runs' && request.method() === 'GET') {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          mode: 'deliver',
          items: [{
            run_id: 'book-1', url: 'https://example.com/course', title: '浏览器导出验收',
            status: 'done', created: '2026-10-30T00:00:00Z', total: 1, done_entries: 1,
          }],
        }),
      })
      return
    }
    if (url.pathname === '/prefs' && request.method() === 'GET') {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [] }) })
      return
    }
    if (url.pathname === '/runs/book-1/note' && request.method() === 'GET') {
      if (notes === 'expired') {
        await route.fulfill({
          status: 410,
          contentType: 'application/json',
          body: JSON.stringify({ error: { code: 410, message: '产物已焚', details: null } }),
        })
        return
      }
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          run_id: 'book-1', status: 'done', notes: [{
            title: '第一讲',
            path: '第一讲.md',
            md: '# 内容\n\n公式：$x^2$。\n\n![[attachments/diagram.png]]',
          }],
        }),
      })
      return
    }
    if (url.pathname === '/runs/book-1/media/diagram.png' && request.method() === 'GET') {
      if (media === 'timeout') {
        await new Promise((resolve) => setTimeout(resolve, 1_000))
      }
      await route.fulfill({ status: 200, contentType: 'image/png', body: PNG_1X1 })
      return
    }
    await route.continue()
  })
}

async function openPdfPopup(page) {
  await expect(page.locator('article.book-card')).toHaveCount(1)
  await page.locator('article.book-card').getByTitle('导出书籍').click()
  await expect(page.getByRole('button', { name: /PDF（打印保存）/ })).toBeVisible()
  const popupPromise = page.waitForEvent('popup')
  await page.getByRole('button', { name: /PDF（打印保存）/ }).click()
  return popupPromise
}

test('prints a book in a real browser with local MathJax and embedded media', async ({ page }) => {
  await installApiFixture(page)
  const externalMathRequests = []
  page.context().on('request', (request) => {
    if (/jsdelivr|mathjax\.org|wicked-good-xpath/i.test(request.url())) externalMathRequests.push(request.url())
  })
  await page.goto('/library')

  const popup = await openPdfPopup(page)
  await popup.waitForLoadState('domcontentloaded')
  await expect(popup.locator('.pdf-document')).toBeVisible()
  await expect(popup.getByRole('heading', { name: /第一讲/ })).toBeVisible()
  await expect(popup.locator('img[src^="blob:"]')).toHaveCount(1)
  await expect(popup.locator('script[src^="/vendor/mathjax/"]')).toHaveCount(1)
  await expect(popup.locator('script[src^="/vendor/mathjax/"]')).toHaveAttribute('src', '/vendor/mathjax/tex-svg.js')
  await expect(popup.locator('script[src*="jsdelivr"]')).toHaveCount(0)
  await expect.poll(() => popup.evaluate(() => Number(window.__v2pdfPrintCalls || 0)), { timeout: 15_000 }).toBe(1)
  expect(externalMathRequests).toEqual([])
})

test('explains that deliver artifacts expired when notes return 410', async ({ page }) => {
  await installApiFixture(page, { notes: 'expired' })
  await page.goto('/library')

  const popupPromise = openPdfPopup(page)
  const popup = await popupPromise
  await expect(page.getByText(/过期|已焚|重新生成/)).toBeVisible()
  await expect.poll(() => popup.isClosed()).toBe(true)
})

test('shows a timeout message when an authenticated media request stalls', async ({ page }) => {
  await installApiFixture(page, { media: 'timeout' })
  await page.goto('/library')

  const popupPromise = openPdfPopup(page)
  const popup = await popupPromise
  await expect(page.getByText(/超时|网络|重试/)).toBeVisible({ timeout: 5_000 })
  await expect.poll(() => popup.isClosed()).toBe(true)
})
