import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

let vite, TaskCard, Queue, useQueueStore
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
before(async () => {
  Object.defineProperty(globalThis, 'localStorage', { configurable: true,
    value: { getItem: () => null, setItem() {}, removeItem() {} } })
  vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, plugins: [vue()], server: { middlewareMode: true } })
  TaskCard = (await vite.ssrLoadModule('/src/components/queue/TaskCard.vue')).default
  Queue = (await vite.ssrLoadModule('/src/views/QueueView.vue')).default
  ;({ useQueueStore } = await vite.ssrLoadModule('/src/stores/queue.ts'))
})
after(async () => {
  await vite?.close()
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else delete globalThis.localStorage
})

const run = (overrides) => ({ run_id: 'dd586072', title: '真实合集前两集', status: 'queued',
  progress: 0, queue_pos: 1, readable: false, bundle_ready: false, ...overrides })
const renderCard = (overrides) => renderToString(createSSRApp(TaskCard, { run: run(overrides) }).use(createPinia()))

for (const status of ['queued', 'running']) {
  test(`${status} cannot expose a stale read/download action`, async () => {
    const html = await renderCard({ status, readable: true, bundle_ready: true })
    assert.match(html, /dd586072/)
    assert.match(html, status === 'queued' ? /等待中/ : /处理中/)
    assert.doesNotMatch(html, /立即阅读|转换完成|部分完成|>\s*下载\s*</)
  })
}

test('zero model usage and a long remote wait remain pending settlement', async () => {
  const html = await renderCard({ status: 'running', cost_usd: null,
    usage: { llm_in_tok: 0, llm_out_tok: 0, gpu_s: 1200.72 } })
  assert.match(html, /输入 0 tokens/)
  assert.match(html, /GPU 1200\.720 秒/)
  assert.match(html, /待结算/)
  assert.doesNotMatch(html, /\$0\.000000|立即阅读|转换完成/)
})

test('failed clips with no readable artifact expose recovery instead of a book', async () => {
  const html = await renderCard({ status: 'partial', diagnostic: {
    stage: 'clips', message: '视频剪辑未完成，暂时无法生成教材；请补跑失败条目。' } })
  assert.match(html, /生成受阻/)
  assert.match(html, /role="alert"/)
  assert.match(html, /clips/)
  assert.match(html, /尚未生成可读教材|补跑失败条目/)
  assert.doesNotMatch(html, /立即阅读|转换完成/)
})

test('healthy completed output still exposes reading and its settled attribution', async () => {
  const html = await renderCard({ status: 'done', readable: true, cost_usd: 0.010311,
    usage: { llm_in_tok: 15939, llm_out_tok: 15841, gpu_s: 1276.45 } })
  assert.match(html, /转换完成/)
  assert.match(html, /立即阅读/)
  assert.match(html, /\$0\.010311/)
  assert.doesNotMatch(html, /尚未生成可读教材|待结算/)
})

test('only an explicit frontend URL submission creates the collection task', async () => {
  const pinia = createPinia()
  const router = createRouter({ history: createMemoryHistory(), routes:
    ['/tasks', '/profile', '/library'].map(path => ({ path, component: Queue })) })
  await router.push('/tasks')
  await router.isReady()
  let state
  const captured = { ...Queue, setup(props, context) { state = Queue.setup(props, context); return state } }
  await renderToString(createSSRApp(captured).use(pinia).use(router))
  const submitted = []
  useQueueStore(pinia).createRun = async (url) => { submitted.push(url) }
  assert.equal(state.quickUrl.value, '')
  await state.quickSubmit()
  assert.deepEqual(submitted, [])
  state.quickUrl.value = '  https://www.bilibili.com/video/BV1LV411r77C/  '
  await state.quickSubmit()
  assert.deepEqual(submitted, ['https://www.bilibili.com/video/BV1LV411r77C/'])
  assert.equal(state.quickUrl.value, '')
})
