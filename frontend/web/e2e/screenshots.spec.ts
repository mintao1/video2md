import { expect, signIn, test } from './fixtures'

// README screenshots: `npm run screenshots`. Runs against the same real-API
// fixture as the E2E suite, so every image shows wired, non-mock UI.
const OUT = '../../docs/screenshots'

test('capture README screenshots', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '开始使用视书' })).toBeVisible()
  await page.screenshot({ path: `${OUT}/brand.png` })

  await signIn(page)
  await page.goto('/library')
  await expect(page.getByRole('button', { name: /开始精读|继续阅读/ }).first()).toBeVisible()
  await page.screenshot({ path: `${OUT}/library.png` })

  await page.goto('/tasks')
  await expect(page.getByRole('main').getByRole('heading', { level: 3 }).first()).toBeVisible()
  await page.screenshot({ path: `${OUT}/tasks.png` })

  await page.goto('/read/finished')
  await expect(page.locator('article.prose-v2md img')).toHaveAttribute('src', /^blob:/)
  await page.screenshot({ path: `${OUT}/reader.png` })

  await page.goto('/profile')
  await expect(page.getByText('Colab 授权')).toBeVisible()
  await page.screenshot({ path: `${OUT}/profile.png` })
})
