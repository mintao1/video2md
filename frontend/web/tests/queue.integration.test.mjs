import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { after, afterEach, before, beforeEach, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

const webRoot = fileURLToPath(new URL('..', import.meta.url))
const repoRoot = resolve(webRoot, '../..')
let backend, vite, temp, baseUrl, api, useQueueStore
let backendLog = ''
let spawnError
const storage = new Map()
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

async function http(path, init = {}) {
  return fetch(`${baseUrl}${path}`, {
    signal: AbortSignal.timeout(3000),
    ...init,
    headers: { Authorization: 'Bearer integration-alice', ...init.headers },
  })
}

async function eventually(check) {
  const deadline = Date.now() + 2000
  while (true) {
    try { check(); return } catch (error) {
      if (Date.now() >= deadline) throw error
      await delay(20)
    }
  }
}

async function waitForBackend() {
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    if (spawnError) throw spawnError
    if (backend.exitCode !== null) throw new Error(`Backend exited: ${backendLog}`)
    if (baseUrl) {
      try {
        if ((await fetch(`${baseUrl}/healthz`, { signal: AbortSignal.timeout(1000) })).ok) return
      } catch { /* socket bound before uvicorn starts accepting */ }
    }
    await delay(50)
  }
  throw new Error(`Backend startup timed out: ${backendLog}`)
}

before(async () => {
  temp = await mkdtemp(resolve(tmpdir(), 'v2md-web-integration-'))
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
  })
  backend = spawn(process.env.V2MD_TEST_PYTHON || resolve(repoRoot, 'backend/.venv/bin/python'), [
    resolve(repoRoot, 'backend/tests/fixtures/web_integration_api.py'), temp,
  ], { cwd: repoRoot, stdio: ['ignore', 'pipe', 'pipe'] })
  backend.on('error', (error) => { spawnError = error })
  backend.stdout.on('data', (chunk) => {
    backendLog += chunk
    const line = backendLog.split('\n').find((value) => value.startsWith('{"base_url":'))
    if (line) baseUrl = JSON.parse(line).base_url
  })
  backend.stderr.on('data', (chunk) => { backendLog += chunk })
  await waitForBackend()
  // Vite performs the same TS/import.meta.env transformations as the application.
  // The store, API facade, live adapters, Pinia and HTTP client are all real modules.
  vite = await createServer({
    root: webRoot, configFile: false, plugins: [vue()], server: { middlewareMode: true },
    define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify(baseUrl) },
  })
  api = await vite.ssrLoadModule('/src/api/index.ts')
  ;({ useQueueStore } = await vite.ssrLoadModule('/src/stores/queue.ts'))
}, { timeout: 20_000 })

beforeEach(async () => {
  storage.clear()
  storage.set('v2md_token', 'integration-alice')
  setActivePinia(createPinia())
  assert.equal((await http('/__tests/reset', { method: 'POST' })).status, 200)
})

afterEach(() => useQueueStore?.().stopProgressTick())

after(async () => {
  await vite?.close()
  if (backend?.pid && backend.exitCode === null && backend.signalCode === null) {
    const exited = once(backend, 'exit')
    backend.kill('SIGTERM')
    const force = setTimeout(() => backend.kill('SIGKILL'), 3000)
    try { await exited } finally { clearTimeout(force) }
  }
  if (temp) await rm(temp, { recursive: true, force: true })
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else delete globalThis.localStorage
})

describe('queue store → HTTP client → FastAPI', () => {
  it('loads only the signed-in owner’s tasks and real progress', async () => {
    const store = useQueueStore()
    await store.load()
    assert.deepEqual(store.runs.map((r) => r.run_id).sort(), ['active', 'finished', 'queued'])
    assert.equal(store.runs.find((r) => r.run_id === 'active').progress, 99)
    assert.equal(store.runs.find((r) => r.run_id === 'queued').status, 'queued')
  })

  it('keeps tasks unchanged when time passes without backend progress', async (t) => {
    const store = useQueueStore()
    await store.load()
    t.mock.method(Math, 'random', () => 1)
    t.mock.timers.enable({ apis: ['setInterval'] })
    store.startProgressTick()
    t.mock.timers.tick(3000)
    await delay(100)
    assert.equal(store.runs.find((r) => r.run_id === 'active').status, 'running')
    assert.equal(store.runs.find((r) => r.run_id === 'active').progress, 99)
    assert.equal(store.runs.find((r) => r.run_id === 'queued').status, 'queued')
  })

  it('observes a backend completion during automatic refresh', async (t) => {
    const store = useQueueStore()
    await store.load()
    t.mock.method(Math, 'random', () => 0)
    t.mock.timers.enable({ apis: ['setInterval'] })
    store.startProgressTick()
    assert.equal((await http('/__tests/complete', { method: 'POST' })).status, 200)
    t.mock.timers.tick(3000)
    await eventually(() => {
      assert.equal(store.runs.find((r) => r.run_id === 'active').status, 'done')
      assert.equal(store.runs.find((r) => r.run_id === 'active').progress, 100)
    })
  })

  it('cancels a queued task persistently through HTTP', async () => {
    await api.cancelRun('queued')
    const store = useQueueStore()
    await store.load()
    assert.equal(store.runs.find((r) => r.run_id === 'queued').status, 'error')
    const detail = await (await http('/runs/queued')).json()
    assert.equal(detail.status, 'error')
  })

  it('does not present cancellation as a resumable pause', async () => {
    const store = useQueueStore()
    await store.load()
    await store.pause('queued')
    assert.equal(store.runs.find((r) => r.run_id === 'queued').status, 'error')
  })

  it('rejects cancellation of a finished task without changing its status', async () => {
    await assert.rejects(() => api.cancelRun('finished'), { name: 'ApiRequestError', code: 409 })
    assert.equal((await (await http('/runs/finished')).json()).status, 'done')
  })

  it('rejects another owner’s cancellation and preserves their task', async () => {
    await assert.rejects(() => api.cancelRun('other'), { name: 'ApiRequestError', code: 403 })
    const response = await http('/runs/other', { headers: { Authorization: 'Bearer integration-bob' } })
    assert.equal((await response.json()).status, 'queued')
  })

  it('rejects requests without usable credentials', async () => {
    storage.delete('v2md_token')
    await assert.rejects(() => api.listRuns(), { name: 'ApiRequestError', code: 401 })
  })

  it('keeps a deleted task absent after a fresh backend list', async () => {
    const store = useQueueStore()
    await store.load()
    store.toggleSelect('finished')
    await store.removeSelected()
    await store.load()
    assert.equal(store.runs.some((r) => r.run_id === 'finished'), false)
  })

  it('rejects retrying a completed task', async () => {
    const store = useQueueStore()
    await store.load()
    await assert.rejects(() => store.resume('finished'))
    assert.equal(store.runs.find((r) => r.run_id === 'finished').status, 'done')
  })

  it('persists queue promotion and returns its new position', async () => {
    const store = useQueueStore()
    await store.load()
    await store.promote('queued')
    await store.load()
    assert.equal(store.runs.find((r) => r.run_id === 'queued').queue_pos, 1)
    await assert.rejects(() => store.promote('active'), { code: 409 })
  })

  it('retries a cancelled task and persists its queued state', async () => {
    const store = useQueueStore()
    await store.load()
    await store.pause('queued')
    await store.resume('queued')
    assert.equal(store.runs.find((r) => r.run_id === 'queued').status, 'queued')
    assert.equal((await (await http('/runs/queued')).json()).status, 'queued')
  })

  it('rejects deleting active work or changing another owner’s task', async () => {
    await assert.rejects(() => api.deleteRuns(['active']), { code: 409 })
    await assert.rejects(() => api.deleteRuns(['other']), { code: 403 })
    await assert.rejects(() => api.resumeRun('other'), { code: 403 })
    await assert.rejects(() => api.promoteRun('other'), { code: 403 })
  })
})

describe('library live adapter → FastAPI preferences and notes', () => {
  it('persists favorites and progress across independent list requests', async () => {
    await api.getBooks()
    await api.toggleFav('finished')
    await api.saveProgress('finished', 42)
    const book = (await api.getBooks()).find((b) => b.id === 'finished')
    assert.equal(book.fav, true)
    assert.equal(book.progress, 42)
    assert.deepEqual((await (await http('/prefs')).json()).items, [
      { run_id: 'finished', fav: true, progress: 42 },
    ])
  })

  it('does not disclose Alice’s preferences to Bob', async () => {
    await api.saveProgress('finished', 42)
    storage.set('v2md_token', 'integration-bob')
    assert.deepEqual(await api.getBooks(), [])
    const response = await http('/prefs', { headers: { Authorization: 'Bearer integration-bob' } })
    assert.deepEqual((await response.json()).items, [])
  })

  it('loads actual note content from the backend', async () => {
    assert.deepEqual(await api.getNotes('finished'), [
      { title: 'Lesson 1', md: '# Integration lesson\n\n![[attachments/demo.png]]' },
    ])
  })
})


it('fetches referenced media as an authenticated binary response', async () => {
  const blob = await api.getMedia('finished', 'demo.png')
  assert.equal(blob.type, 'image/png')
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer()).slice(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10])
  storage.set('v2md_token', 'integration-bob')
  await assert.rejects(() => api.getMedia('finished', 'demo.png'), { code: 403 })
})

it('the actual getMedia adapter uses authenticated ranges through FastAPI for MP4', async () => {
  assert.equal((await http('/__tests/seed-range-media', { method: 'POST' })).status, 200)
  const blob = await api.getMedia('finished', 'range-demo.mp4')
  assert.equal(blob.type, 'video/mp4')
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()),
    Uint8Array.from({ length: 150000 }, (_, i) => i % 251))
  const auditResponse = await http('/__tests/range-audit', { method: 'POST' })
  assert.equal(auditResponse.status, 200)
  const audit = (await auditResponse.json()).requests
  assert.deepEqual(audit.map(row => row.range), ['bytes=0-65535', 'bytes=65536-131071', 'bytes=131072-196607'])
  assert.ok(audit.every(row => row.authenticated && row.status === 206))
  assert.ok(audit.slice(1).every(row => row.if_range === audit[1].if_range && row.if_range))
  storage.set('v2md_token', 'integration-bob')
  await assert.rejects(() => api.getMedia('finished', 'range-demo.mp4'), { code: 403 })
  const denied = (await (await http('/__tests/range-audit', { method: 'POST' })).json()).requests.at(-1)
  assert.equal(denied.status, 403)
})

it('reports unavailable bundles instead of a fake successful download', async () => {
  await assert.rejects(() => api.downloadBundle('finished'), { code: 409 })
})

it('persists private LLM settings through the live API without returning or caching its key', async () => {
  storage.set('v2md_token', 'integration-alice')
  try {
    const saved = await api.saveLlmSettings({
      base_url: 'https://llm.example.test/v1', api_key: 'fixture-only-secret', model: 'test-model',
    })
    assert.equal(saved.configured, true)
    assert.equal(saved.has_api_key, true)
    assert.doesNotMatch(JSON.stringify(saved), /fixture-only-secret/)
    const persisted = JSON.parse(await readFile(resolve(temp, 'llm-settings.json'), 'utf8'))
    assert.match(persisted.alice.api_key, /^v1:aesgcm:/)
    assert.doesNotMatch(persisted.alice.api_key, /fixture-only-secret/)
    assert.equal((await api.getLlmSettings()).base_url, 'https://llm.example.test/v1')
    await api.saveLlmSettings({ base_url: 'https://llm.example.test/v2', model: 'new-model' })
    assert.equal((await api.getLlmSettings()).has_api_key, true)
    storage.set('v2md_token', 'integration-bob')
    assert.equal((await api.getLlmSettings()).configured, false)
    storage.set('v2md_token', 'integration-alice')
    assert.equal([...storage.values()].includes('fixture-only-secret'), false)
    await api.clearLlmSettings()
    assert.equal((await api.getLlmSettings()).has_api_key, false)
  } finally {
    storage.set('v2md_token', 'integration-alice')
    await api.clearLlmSettings()
  }
})


it('preserves real run usage and redacted failure diagnostics through HTTP, adapter and rendered card', async () => {
  assert.equal((await http('/__tests/model-failure', { method: 'POST' })).status, 200)
  const run = (await api.listRuns()).find((r) => r.run_id === 'finished')
  assert.equal(run.usage.gpu_s, 204.669)
  assert.equal(run.cost_usd, 0)
  assert.equal(run.diagnostic.stage, 'note')
  assert.match(run.diagnostic.message, /403/)
  assert.doesNotMatch(JSON.stringify(run), /sk-private|upstream-secret/)
  const Component = (await vite.ssrLoadModule('/src/components/queue/TaskCard.vue')).default
  const app = createSSRApp(Component, { run })
  app.use(createPinia())
  const html = await renderToString(app)
  assert.match(html, /任务编号：finished/)
  assert.match(html, /失败阶段：note/)
  assert.match(html, /204.669/)
  assert.match(html, /403/)
})

it('loads actual monthly billing details and renders per-run usage instead of an inert placeholder', async () => {
  const report = await api.getCost()
  assert.deepEqual(report.runs.map((r) => r.run_id), ['finished'])
  assert.equal(report.runs[0].usage.llm_in_tok, 1000)
  assert.equal(report.spend_usd, 0.00028)
  const Component = (await vite.ssrLoadModule('/src/components/profile/BillingPanel.vue')).default
  const html = await renderToString(createSSRApp(Component, { report }))
  assert.match(html, /费用明细/)
  assert.match(html, /任务编号：finished/)
  assert.match(html, /1000 tokens/)
  assert.match(html, /180.000 秒/)
  assert.match(html, /\$0.000280/)
  storage.set('v2md_token', 'integration-bob')
  assert.deepEqual((await api.getCost()).runs, [])
})

it('keeps a billed partial run without a note out of the library and disables reading', async () => {
  await http('/__tests/quality-partial', { method: 'POST' })
  const run = (await api.listRuns()).find((r) => r.run_id === 'finished')
  assert.equal(run.readable, false)
  assert.match(run.diagnostic.message, /过短/)
  assert.deepEqual(await api.getBooks(), [])
  const store = useQueueStore()
  await store.load()
  assert.equal(store.counts.done, 0)
  assert.equal(store.counts.error, 1)
  const Component = (await vite.ssrLoadModule('/src/components/queue/TaskCard.vue')).default
  const app = createSSRApp(Component, { run })
  app.use(createPinia())
  const html = await renderToString(app)
  assert.doesNotMatch(html, /立即阅读/)
  assert.match(html, /尚未生成可读教材/)
  assert.match(html, /过短/)
})

it('reuses a queued submission without duplicating the frontend row', async () => {
  const store = useQueueStore()
  await store.load()
  const before = store.runs.length
  await store.createRun('https://example.test/queued')
  await store.createRun('https://example.test/queued')
  assert.equal(store.runs.length, before)
  assert.equal(store.runs.filter((r) => r.run_id === 'queued').length, 1)
})

it('renders API rejection persistently inside the still-open import sheet', async () => {
  const Component = (await vite.ssrLoadModule('/src/components/queue/ImportModal.vue')).default
  const context = {}
  await renderToString(createSSRApp(Component, { modelValue: true, error: '提交失败：本地路径输入已被禁用' }), context)
  assert.match(context.teleports.body, /role="alert"[^>]*>提交失败：本地路径输入已被禁用/)
  assert.match(context.teleports.body, /添加新的网课转换/)
})

for (const code of [422, 429]) {
  it(`preserves tasks and billing on an actual HTTP ${code} rejection and renders its message`, async () => {
    const store = useQueueStore()
    await store.load()
    const beforeRows = JSON.stringify(store.runs)
    const beforeCost = await api.getCost()
    const url = code === 422 ? '/not-an-approved-upload.mp4' : 'https://example.test/budget-rejected'
    if (code === 429) {
      assert.equal((await http('/__tests/budget', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: true }),
      })).status, 200)
    }
    try {
      let failure
      await assert.rejects(() => store.createRun(url), (error) => {
        failure = error
        return error.name === 'ApiRequestError' && error.code === code
      })
      assert.match(failure.message, code === 422 ? /本地路径输入已被禁用/ : /本月预算已用尽/)
      assert.equal(JSON.stringify(store.runs), beforeRows)
      await store.load()
      assert.equal(JSON.stringify(store.runs), beforeRows)
      const afterCost = await api.getCost()
      assert.deepEqual(afterCost.usage, beforeCost.usage)
      assert.deepEqual(afterCost.runs, beforeCost.runs)
      assert.equal(afterCost.spend_usd, beforeCost.spend_usd)
      const Component = (await vite.ssrLoadModule('/src/components/queue/ImportModal.vue')).default
      const context = {}
      await renderToString(createSSRApp(Component, { modelValue: true, error: failure.message }), context)
      assert.match(context.teleports.body, /role="alert"/)
      assert.ok(context.teleports.body.includes(failure.message))
      assert.match(context.teleports.body, /添加新的网课转换/)
    } finally {
      if (code === 429) {
        await http('/__tests/budget', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: false }),
        })
        assert.equal((await api.getCost()).budget_usd, beforeCost.budget_usd)
      }
    }
  })
}
