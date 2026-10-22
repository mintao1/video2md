import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

let vite, Queue, ImportModal, useQueueStore
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
before(async () => {
  Object.defineProperty(globalThis, 'localStorage', { configurable: true,
    value: { getItem: () => null, setItem() {}, removeItem() {} } })
  vite = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, plugins: [vue()], server: { middlewareMode: true } })
  Queue = (await vite.ssrLoadModule('/src/views/QueueView.vue')).default
  ImportModal = (await vite.ssrLoadModule('/src/components/queue/ImportModal.vue')).default
  ;({ useQueueStore } = await vite.ssrLoadModule('/src/stores/queue.ts'))
})
after(async () => {
  await vite?.close()
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else delete globalThis.localStorage
})

test('an unresolved submission visibly waits, blocks duplicates, then restores the input on failure', async () => {
  const pinia = createPinia()
  const router = createRouter({ history: createMemoryHistory(), routes:
    ['/tasks', '/profile', '/library'].map(path => ({ path, component: Queue })) })
  await router.push('/tasks')
  await router.isReady()
  let state
  const capture = { ...Queue, setup(props, context) { state = Queue.setup(props, context); return state } }
  const app = (component) => createSSRApp(component).use(pinia).use(router)
  await renderToString(app(capture))
  const store = useQueueStore(pinia)
  let reject, calls = 0
  store.createRun = () => { calls++; return new Promise((_, fail) => { reject = fail }) }
  state.quickUrl.value = 'https://www.bilibili.com/video/BV1vV411k7GB/'
  const request = state.quickSubmit()
  await state.quickSubmit()
  await state.startConvert({ url: 'another video' })
  assert.equal(calls, 1)
  const render = () => renderToString(app({ ...Queue, setup: () => state }))
  const pending = await render()
  assert.match(pending, /<button[^>]*disabled[^>]*aria-busy="true"/)
  assert.match(pending, /正在解析…/)
  assert.match(pending, /role="status"[^>]*[\s\S]*正在读取视频与合集信息/)
  assert.match(pending, /<input[^>]*disabled/)
  reject(new Error('公开视频接口暂时不可用'))
  await request
  const failed = await render()
  assert.match(failed, /提交失败：公开视频接口暂时不可用/)
  assert.match(failed, /BV1vV411k7GB/)
  assert.doesNotMatch(failed, /aria-busy="true"|正在解析…/)
})

test('the import sheet exposes pending feedback and disables its submit action', async () => {
  const context = {}
  await renderToString(createSSRApp(ImportModal, { modelValue: true, busy: true }), context)
  const html = Object.values(context.teleports || {}).join('')
  assert.match(html, /正在解析…/)
  assert.match(html, /<button[^>]*disabled[^>]*aria-busy="true"/)
  assert.match(html, /role="status"/)
})
