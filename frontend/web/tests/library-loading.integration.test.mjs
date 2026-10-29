import assert from 'node:assert/strict'
import { after, before, it } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

let vite
const storage = new Map()

before(async () => {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
  })
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    plugins: [vue()],
    server: { middlewareMode: true },
  })
})

after(async () => {
  await vite?.close()
  delete globalThis.localStorage
})

it('does not expose an empty library count while books are still loading', async () => {
  const LibraryView = (await vite.ssrLoadModule('/src/views/LibraryView.vue')).default
  const { useLibraryStore } = await vite.ssrLoadModule('/src/stores/library.ts')
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useLibraryStore()
  store.loading = true
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: LibraryView }],
  })
  const app = createSSRApp(LibraryView)
  app.use(pinia)
  app.use(router)
  const html = await renderToString(app)
  assert.match(html, /正在同步教材/)
  assert.doesNotMatch(html, /共 0 本/)
})
