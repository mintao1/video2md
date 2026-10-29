import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import * as Vue from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { renderToString } from '@vue/server-renderer'

let vite, Brand, Colab, LlmSettings
const originals = new Map(['window', 'document', 'localStorage', 'fetch'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
const setGlobal = (key, value) => Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], server: { middlewareMode: true, hmr: false } })
  Brand = (await vite.ssrLoadModule('/src/views/BrandView.vue')).default
  Colab = (await vite.ssrLoadModule('/src/views/ColabAuthView.vue')).default
  LlmSettings = (await vite.ssrLoadModule('/src/components/profile/LlmSettingsCard.vue')).default
})
after(async () => {
  await vite?.close()
  for (const [key, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else delete globalThis[key]
  }
})

function hostRenderer() {
  const node = (type, text = '') => ({ type, text, children: [], parent: null })
  return { root: node('root'), host: Vue.createRenderer({
    createElement: node, createText: text => node('text', text), createComment: text => node('comment', text),
    setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text },
    patchProp() {}, parentNode: el => el.parent, nextSibling: () => null,
    insert(el, parent) { el.parent = parent; parent.children.push(el) }, remove() {},
  }) }
}
async function mounted(Component, location, respond, fragment = '#id_token=website-id-token') {
  const storage = new Map()
  let fragmentCleared = false
  const calls = []
  setGlobal('localStorage', { getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) })
  const navigations = []
  setGlobal('window', { location: { hash: fragment, pathname: '/', search: '', assign: url => navigations.push(url) },
    history: { replaceState() { fragmentCleared = true } } })
  setGlobal('document', { title: 'test' })
  setGlobal('fetch', async (url, init) => {
    calls.push({ url, init })
    return respond(url, init)
  })
  let state
  const Capture = { setup(props, context) {
    state = Component.setup(props, context)
    return () => Vue.h('main')
  } }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: Capture },
    ...['/library', '/tasks', '/profile/colab'].map(path => ({ path, component: { render: () => null } })),
  ] })
  await router.push(location)
  const { root, host } = hostRenderer()
  const app = host.createApp(Capture).use(createPinia()).use(router)
  app.provide(Vue.ssrContextKey, { modules: new Set() })
  app.mount(root)
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve))
  return { state, router, calls, storage, fragmentCleared, navigations, close: () => app.unmount() }
}
const json = body => ({ ok: true, json: async () => body })

for (const connected of [false, true]) {
  test(`Google callback ${connected ? 'reuses an existing Colab connection' : 'guides an unconnected tenant and preserves the intended page'}`, async () => {
    const page = await mounted(Brand, '/?redirect=%2Ftasks%3FopenImport%3D1', () => json({ can_authorize: true, has_credentials: connected }))
    try {
      assert.equal(page.storage.get('v2md_token'), 'website-id-token')
      assert.equal(page.fragmentCleared, true)
      assert.match(page.calls[0].url, /\/settings\/colab$/)
      assert.equal(page.calls[0].init.headers.Authorization, 'Bearer website-id-token')
      if (connected) assert.equal(page.router.currentRoute.value.fullPath, '/tasks?openImport=1')
      else {
        assert.equal(page.router.currentRoute.value.path, '/profile/colab')
        assert.equal(page.router.currentRoute.value.query.next, '/tasks?openImport=1')
      }
    } finally { page.close() }
  })
}

test('Colab status being unavailable does not prevent website login', async () => {
  const page = await mounted(Brand, '/', () => { throw new TypeError('offline') })
  try { assert.equal(page.router.currentRoute.value.path, '/library') }
  finally { page.close() }
})

test('Colab onboarding return targets cannot leave the application', async () => {
  const page = await mounted(Colab, '/?next=https%3A%2F%2Foutside.invalid', () => json({ can_authorize: true, has_credentials: false }))
  try {
    assert.equal(page.state.next.value, '/library')
    assert.equal(page.state.onboarding.value, true)
    assert.equal(page.state.status.value.has_credentials, false)
  } finally { page.close() }
})

test('an already requested Colab page does not create a nested onboarding redirect', async () => {
  const page = await mounted(Brand, '/?redirect=%2Fprofile%2Fcolab', () => json({ can_authorize: true, has_credentials: false }))
  try { assert.equal(page.router.currentRoute.value.fullPath, '/profile/colab') }
  finally { page.close() }
})

test('a Google callback rejects an external destination before onboarding', async () => {
  const page = await mounted(Brand, '/?redirect=https%3A%2F%2Foutside.invalid', () => json({ can_authorize: true, has_credentials: false }))
  try {
    assert.equal(page.router.currentRoute.value.path, '/profile/colab')
    assert.equal(page.router.currentRoute.value.query.next, '/library')
  } finally { page.close() }
})

test('an identity without Colab authorization capability can still enter the website', async () => {
  const page = await mounted(Brand, '/?redirect=%2Ftasks', () => json({ can_authorize: false, has_credentials: false }))
  try { assert.equal(page.router.currentRoute.value.fullPath, '/tasks') }
  finally { page.close() }
})

test('agreement is required before starting Google login and the intended page is preserved', async () => {
  const page = await mounted(Brand, '/?redirect=%2Ftasks%3FopenImport%3D1', () => { throw new Error('unexpected fetch') }, '')
  try {
    page.state.startGoogleLogin()
    assert.deepEqual(page.navigations, [])
    assert.match(page.state.error.value, /用户服务协议/)
    page.state.agreed.value = true
    page.state.startGoogleLogin()
    const destination = new URL(page.navigations[0])
    assert.equal(destination.pathname, '/auth/google/login')
    assert.equal(destination.searchParams.get('redirect'), '/tasks?openImport=1')
    assert.equal(destination.searchParams.get('terms_accepted'), '1')
    assert.equal(page.calls.length, 0)
  } finally { page.close() }
})

test('OAuth errors clear the fragment without storing a token or starting Colab authorization', async () => {
  const page = await mounted(Brand, '/', () => { throw new Error('unexpected fetch') }, '#auth_error=Google%20login%20failed')
  try {
    assert.equal(page.state.error.value, 'Google login failed')
    assert.equal(page.fragmentCleared, true)
    assert.equal(page.storage.get('v2md_token'), undefined)
    assert.equal(page.calls.length, 0)
    assert.equal(page.router.currentRoute.value.path, '/')
  } finally { page.close() }
})

test('Colab authorization is explicitly started and completing it clears the entered code', async () => {
  const page = await mounted(Colab, '/?next=%2Ftasks', url => {
    if (url.endsWith('/settings/colab/complete')) return json({ can_authorize: true, has_credentials: true })
    if (url.endsWith('/settings/colab/start')) return json({ flow_id: 'offline-flow', authorization_url: 'https://accounts.google.com/o/oauth2/auth' })
    return json({ can_authorize: true, has_credentials: false })
  })
  try {
    assert.equal(page.calls.length, 1, 'mounting must only read status, never grant permissions')
    window.open = () => null
    await page.state.start()
    assert.equal(page.state.flowId.value, 'offline-flow')
    assert.match(page.state.authorizationUrl.value, /^https:\/\/accounts\.google\.com\//)
    page.state.code.value = 'offline-authorization-code'
    await page.state.complete()
    assert.equal(page.state.status.value.has_credentials, true)
    assert.equal(page.state.code.value, '')
    assert.equal(page.state.flowId.value, '')
    assert.equal(page.state.authorizationUrl.value, '')
    assert.equal(page.state.next.value, '/tasks')
    assert.match(page.state.message.value, /仅用于当前账号/)
    assert.equal(JSON.parse(page.calls.at(-1).init.body).code, 'offline-authorization-code')
    assert.ok([...page.storage.values()].every(value => !value.includes('offline-authorization-code')))
  } finally { page.close() }
})

test('failed Colab exchange clears the one-time code and requires a fresh flow', async () => {
  const page = await mounted(Colab, '/', url => url.endsWith('/complete')
    ? { ok: false, status: 400, statusText: 'Bad Request', json: async () => ({ error: { code: 'HTTP_400', message: '请选择相同的 Google 账号' } }) }
    : json({ can_authorize: true, has_credentials: false }))
  try {
    page.state.flowId.value = 'offline-flow'
    page.state.authorizationUrl.value = 'https://accounts.google.com/o/oauth2/auth'
    page.state.code.value = 'offline-authorization-code'
    await page.state.complete()
    assert.equal(page.state.status.value.has_credentials, false)
    assert.equal(page.state.code.value, '')
    assert.equal(page.state.flowId.value, '')
    assert.equal(page.state.authorizationUrl.value, '')
    assert.match(page.state.error.value, /相同的 Google 账号/)
    assert.equal(page.state.message.value, '')
  } finally { page.close() }
})

test('the rendered LLM card places the personal Colab and official Kaggle links below both input fields', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes:
    ['/profile', '/profile/colab'].map(path => ({ path, component: { render: () => null } })) })
  await router.push('/profile')
  const html = await renderToString(Vue.createSSRApp(LlmSettings).use(router))
  const formEnd = html.indexOf('</form>'), nav = html.indexOf('<nav')
  assert.ok(html.indexOf('id="llm-api-url"') < formEnd)
  assert.ok(html.indexOf('id="llm-api-key"') < formEnd)
  assert.ok(formEnd >= 0 && nav > formEnd, 'both links must follow the complete LLM form')
  const links = html.slice(nav)
  assert.match(links, /href="\/profile\/colab"[^>]*>\s*登录 Colab/)
  assert.match(links, /href="https:\/\/www\.kaggle\.com\/account\/login"[^>]*target="_blank"[^>]*rel="noopener noreferrer"[^>]*>\s*登录 Kaggle/)
  assert.doesNotMatch(links, /colab\.research\.google\.com|服务管理员|服务共用/)
  assert.equal((links.match(/<a\b/g) ?? []).length, 2)
})

test('the rendered personal Colab page explains consent and offers skip or continue according to connection state', async () => {
  for (const connected of [false, true]) {
    const router = createRouter({ history: createMemoryHistory(), routes:
      ['/profile/colab', '/profile', '/tasks', '/library'].map(path => ({ path, component: { render: () => null } })) })
    await router.push('/profile/colab?next=%2Ftasks')
    const Page = { ...Colab, setup(props, context) {
      const state = Colab.setup(props, context)
      state.loading.value = false
      state.status.value = { can_authorize: true, has_credentials: connected }
      return state
    } }
    const html = await renderToString(Vue.createSSRApp(Page).use(router))
    assert.match(html, /与当前登录相同的 Google 账号/)
    assert.match(html, /首次需要在 Google 页面同意 Colab 权限/)
    assert.match(html, /再次登录视书可复用已保存的授权/)
    assert.doesNotMatch(html, /服务共用|仅服务管理员/)
    assert.match(html, connected ? /href="\/tasks"[^>]*>继续使用/ : /href="\/tasks"[^>]*>稍后连接，继续使用/)
  }
})
