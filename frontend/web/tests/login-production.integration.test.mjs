import assert from 'node:assert/strict'
import { access, readFile, readdir } from 'node:fs/promises'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const repo = fileURLToPath(new URL('../../..', import.meta.url))

test('brand home owns the OAuth entry and the standalone login page stays deleted', async () => {
  const router = await readFile(`${root}/src/router/index.ts`, 'utf8')
  const view = await readFile(`${root}/src/views/BrandView.vue`, 'utf8')

  await assert.rejects(access(`${root}/src/views/LoginView.vue`))
  await assert.rejects(access(`${root}/src/views/ProductionLoginView.vue`))
  assert.match(router, /path: '\/'[\s\S]*BrandView\.vue/)
  assert.doesNotMatch(router, /path: '\/login'/)
  assert.match(view, />视书</)
  assert.match(view, /视频一键转成可精读的教材/)
  assert.match(view, /LogoMark/)
  assert.match(view, /\/auth\/google\/login/)
  assert.match(view, /reason === 'expired'/)
  assert.match(view, /safeRedirect/)
  assert.match(view, /id_token/)
  assert.match(view, /auth_error/)
  assert.doesNotMatch(view, /手机号|验证码|密码登录|微信一键登录|mock/i)
})

test('the tenant login evaluation set preserves brand contracts and covers personal Colab connections', async () => {
  const text = await readFile(`${repo}/docs/evals/tenant-colab-login-eval-set.jsonl`, 'utf8')
  const rows = text.trim().split('\n').map((line) => JSON.parse(line))
  assert.deepEqual(rows.map(row => row.id), Array.from({ length: 13 }, (_, i) => `tenant-colab-${String(i + 1).padStart(3, '0')}`))
  assert.ok(rows.every(row => row.input.actions.length && row.expectedOutput.criteria.length && row.counterexamples.length >= 2))
  assert.ok(rows.every(row => row.metadata.offline_can_close_production === false))
  assert.deepEqual(new Set(rows.map(row => row.metadata.area)), new Set([
    'onboarding', 'authorization', 'tenant-isolation', 'account-match', 'execution', 'platform-entry', 'website-auth', 'production-ui-bundle',
  ]))
  const preserved = new Set(rows.flatMap(row => row.metadata.supersedes_ids))
  for (let i = 1; i <= 8; i++) assert.ok(preserved.has(`prod-login-ui-${String(i).padStart(3, '0')}`))
})

test('every authentication exit targets the brand home instead of a standalone login route', async () => {
  const router = await readFile(`${root}/src/router/index.ts`, 'utf8')
  const auth = await readFile(`${root}/src/stores/auth.ts`, 'utf8')
  const client = await readFile(`${root}/src/api/client.ts`, 'utf8')
  const backend = await readFile(`${repo}/backend/video2pdf/api.py`, 'utf8')
  const terms = await readFile(`${root}/src/views/TermsView.vue`, 'utf8')
  const privacy = await readFile(`${root}/src/views/PrivacyView.vue`, 'utf8')
  const readme = await readFile(`${root}/README.md`, 'utf8')

  assert.match(router, /return \{ name: 'brand', query: \{ redirect: to\.fullPath \} \}/)
  assert.match(auth, /window\.location\.replace\('\/'\)/)
  assert.match(client, /window\.location\.assign\(`\/\?\$\{query\}`\)/)
  assert.match(backend, /base = f"\{_google_web_base\(\)\}\/"/)
  assert.match(terms, /to="\/">← 返回品牌首页/)
  assert.match(privacy, /to="\/">← 返回品牌首页/)
  assert.match(readme, /不存在独立 `\/login` 页面/)

  for (const source of [router, auth, client, backend, terms, privacy]) {
    assert.doesNotMatch(source, /(?:path:|replace\(|assign\(|to=|base =).*['"`]\/login/)
  }
})

test('production source contains no mock API or mock authentication route', async () => {
  await assert.rejects(access(`${root}/src/api/mock`))

  const apiIndex = await readFile(`${root}/src/api/index.ts`, 'utf8')
  const router = await readFile(`${root}/src/router/index.ts`, 'utf8')
  const types = await readFile(`${root}/src/types/index.ts`, 'utf8')
  const apiFiles = await readdir(`${root}/src/api/live`)

  assert.deepEqual(apiFiles.sort(), ['books.ts', 'llm-settings.ts', 'profile.ts', 'runs.ts', 'uploads.ts'])
  assert.doesNotMatch(apiIndex, /mock|换源/)
  assert.doesNotMatch(types, /SmsCodeOut|短信验证码 mock/)
  assert.doesNotMatch(router, /mock|sms|wechat|password/i)
  assert.deepEqual(
    [...router.matchAll(/path:\s*'([^']+)'/g)].map((match) => match[1]),
    ['/', '/library', '/read/:id', '/tasks', '/profile/colab', '/profile', '/terms', '/privacy', '/:pathMatch(.*)*'],
  )
})
