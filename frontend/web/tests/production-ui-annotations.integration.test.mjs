import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

let vite, LlmSettings, storage, writes
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
const originalFetch = globalThis.fetch
const canary = 'fixture-only-key-not-a-real-credential'
const url = 'https://llm.example.invalid/v1'
const model = 'fixture-model'
const apps = []
const summary = (configured = false) => ({ base_url: url, model, configured, has_api_key: configured })
const response = (body, status = 200) => new Response(JSON.stringify(body), { status })

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    plugins: [vue()],
    server: { middlewareMode: true, hmr: false },
  })
  LlmSettings = (await vite.ssrLoadModule('/src/components/profile/LlmSettingsCard.vue')).default
})
beforeEach(() => {
  storage = new Map()
  writes = []
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => storage.get(key) ?? null,
      setItem(key, value) {
        writes.push({ key, value })
        storage.set(key, value)
      },
      removeItem: (key) => storage.delete(key),
    },
  })
})
afterEach(() => {
  for (const app of apps.splice(0)) app.unmount()
  globalThis.fetch = originalFetch
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else delete globalThis.localStorage
})
after(async () => {
  await vite?.close()
})

async function mountCard(respond) {
  const calls = []
  globalThis.fetch = async (target, init = {}) => {
    assert.equal(new URL(target).pathname, '/settings/llm', 'never contact an external model')
    calls.push({ method: init.method ?? 'GET', body: init.body && JSON.parse(init.body) })
    return respond(calls.at(-1))
  }
  const node = (type, text = '') => ({ type, text, parent: null, children: [] })
  const renderer = Vue.createRenderer({
    createElement: node,
    createText: (text) => node('text', text),
    createComment: (text) => node('comment', text),
    setText: (element, text) => {
      element.text = text
    },
    setElementText: (element, text) => {
      element.text = text
    },
    patchProp() {},
    parentNode: (element) => element.parent,
    nextSibling: () => null,
    insert(element, parent) {
      element.parent = parent
      parent.children.push(element)
    },
    remove() {},
  })
  let state
  const app = renderer.createApp({
    setup(props, context) {
      state = LlmSettings.setup(props, context)
      return () => Vue.h('main')
    },
  })
  app.provide(Vue.ssrContextKey, { modules: new Set() })
  app.mount(node('root'))
  apps.push(app)
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve))
  return { state, calls }
}

function fill(state) {
  state.baseUrl.value = ` ${url} `
  state.apiKey.value = canary
  state.model.value = ` ${model} `
}

async function renderCard(state) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['/profile', '/profile/colab'].map((path) => ({ path, component: { render: () => null } })),
  })
  await router.push('/profile')
  return renderToString(Vue.createSSRApp({ ...LlmSettings, setup: () => state }).use(router))
}

test('pua-001 submits explicitly entered URL key and model and clears the key after success', async () => {
  const page = await mountCard((call) => response(summary(call.method === 'PUT')))
  assert.equal(page.state.loaded.value, true)
  assert.equal(page.state.hasKey.value, false)
  fill(page.state)
  const enteredHtml = await renderCard(page.state)
  for (const [id, value] of [
    ['llm-api-url', ` ${url} `],
    ['llm-api-key', canary],
    ['llm-model', ` ${model} `],
  ]) {
    const input = enteredHtml.match(new RegExp(`<input id="${id}"[^>]*>`))?.[0]
    assert.ok(input, `the actual template must render ${id}`)
    assert.equal(input.match(/value="([^"]*)"/)?.[1], value, 'input must remain bound to the submitted state')
  }
  await page.state.save()
  assert.deepEqual(
    page.calls.filter((call) => call.method === 'PUT'),
    [{ method: 'PUT', body: { base_url: url, api_key: canary, model } }],
  )
  assert.match(page.state.message.value, /配置已保存/)
  assert.equal(page.state.error.value, '')
  assert.equal(page.state.apiKey.value, '')
  assert.equal(page.state.showKey.value, false)
  assert.equal(page.state.hasKey.value, true)
  assert.equal(page.state.busy.value, false)
  assert.deepEqual(writes, [])
})

test('pua-002 reopens persisted settings without echoing or locally storing the key', async () => {
  let saved = false
  const respond = (call) => {
    if (call.method === 'PUT') {
      assert.equal(call.body.api_key, canary)
      saved = true
    }
    const safe = summary(saved)
    assert.equal(Object.hasOwn(safe, 'api_key'), false)
    return response(safe)
  }
  const first = await mountCard(respond)
  fill(first.state)
  await first.state.save()
  apps.splice(0, 1)[0].unmount()
  const second = await mountCard(respond)
  assert.equal(second.state.baseUrl.value, url)
  assert.equal(second.state.model.value, model)
  assert.equal(second.state.configured.value, true)
  assert.equal(second.state.hasKey.value, true)
  assert.equal(second.state.apiKey.value, '')
  assert.equal(second.state.showKey.value, false)
  assert.deepEqual(
    second.calls.map((call) => call.method),
    ['GET'],
  )
  assert.deepEqual(writes, [])
  assert.ok([...storage.values()].every((value) => !value.includes(canary)))
  const html = await renderCard(second.state)
  assert.match(html, /已保存个人配置/)
  assert.match(html, /id="llm-api-key"[^>]*type="password"/)
  assert.doesNotMatch(html, new RegExp(canary))
})

test('pua-001 save errors never display success or discard the entered configuration', async () => {
  for (const failure of ['http', 'transport']) {
    const page = await mountCard((call) => {
      if (call.method !== 'PUT') return response(summary())
      if (failure === 'transport') throw new TypeError('fixture transport failure')
      return response({ error: { code: 'UNAVAILABLE', message: 'fixture unavailable' } }, 503)
    })
    fill(page.state)
    await page.state.save()
    assert.equal(page.state.message.value, '')
    assert.match(page.state.error.value, /保存失败/)
    assert.equal(page.state.apiKey.value, canary)
    assert.equal(page.state.busy.value, false)
    assert.ok(!page.state.error.value.includes(canary))
  }
})

test('pua-001 missing initial key or unavailable settings cannot submit a save', async () => {
  const empty = await mountCard(() => response(summary()))
  await empty.state.save()
  assert.match(empty.state.error.value, /首次配置请填写 API Key/)
  assert.deepEqual(
    empty.calls.map((call) => call.method),
    ['GET'],
  )
  const unavailable = await mountCard(() => {
    throw new TypeError('fixture offline')
  })
  fill(unavailable.state)
  await unavailable.state.save()
  assert.equal(unavailable.state.loaded.value, false)
  assert.deepEqual(
    unavailable.calls.map((call) => call.method),
    ['GET'],
  )
  assert.equal(unavailable.state.message.value, '')
})
