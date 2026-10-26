import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import * as Vue from 'vue'
import { parse } from '@vue/compiler-sfc'
import { compile } from '@vue/compiler-dom'
import { createPinia } from 'pinia'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createMemoryHistory, createRouter, useRoute, useRouter } from 'vue-router'

// Mount the real App template in a host renderer. No browser or source-regex
// assertions: the router actually consumes its query and reconciles the page.
function renderer() {
  const node = (type, text = '') => ({ type, text, children: [], parent: null })
  return { root: node('root'), host: Vue.createRenderer({
    createElement: node, createText: text => node('text', text), createComment: text => node('comment', text),
    setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text },
    patchProp() {}, parentNode: el => el.parent,
    nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
    insert(el, parent, anchor) {
      if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1)
      el.parent = parent
      const at = anchor ? parent.children.indexOf(anchor) : -1
      if (at < 0) parent.children.push(el)
      else parent.children.splice(at, 0, el)
    },
    remove(el) { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) },
  }) }
}

test('consuming the import query preserves task state while reader query changes still remount', async () => {
  const source = await readFile(process.env.V2PDF_TEST_APP_TEMPLATE_FILE || new URL('../src/App.vue', import.meta.url), 'utf8')
  const template = parse(source).descriptor.template.content
  const render = new Function('Vue', compile(template, { mode: 'function', prefixIdentifiers: true }).code)(Vue)
  let mounts = 0, readerMounts = 0, opened
  const Tasks = { setup() {
    mounts++
    const route = useRoute(), router = useRouter()
    opened = Vue.ref(false)
    const consumeImport = () => {
      if (route.query.openImport === '1') { opened.value = true; void router.replace({ query: {} }) }
    }
    Vue.watch(() => route.query.openImport, consumeImport)
    Vue.onMounted(consumeImport)
    return () => Vue.h('main', opened.value ? 'import-open' : 'import-closed')
  } }
  const Reader = { setup() { readerMounts++; return () => Vue.h('main', 'reader') } }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/tasks', name: 'tasks', component: Tasks },
    { path: '/read/:id', name: 'read', component: Reader },
  ] })
  await router.push('/tasks')
  const { root, host } = renderer()
  const app = host.createApp({ render, components: { ToastHost: { render: () => null } } }).use(router)
  app.mount(root)
  await Vue.nextTick()
  await router.push('/tasks?openImport=1')
  // The page watcher deliberately clears the query in a second navigation.
  await new Promise(resolve => setImmediate(resolve))
  await Vue.nextTick()
  assert.deepEqual(router.currentRoute.value.query, {})
  assert.equal(mounts, 1, 'query consumption must not discard the open importer')
  assert.equal(opened.value, true)
  await router.push('/read/book?chapter=1')
  await Vue.nextTick()
  await router.push('/read/book?chapter=2')
  await Vue.nextTick()
  assert.equal(readerMounts, 2, 'reader chapter navigation keeps its previous remount contract')
  app.unmount()
})

test('the actual QueueView opens on direct import and on repeated same-route navigation', async () => {
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true,
    value: { getItem: () => null, setItem() {}, removeItem() {} } })
  const vite = await createServer({ configFile: false, plugins: [vue()],
    server: { middlewareMode: true, hmr: false } })
  let app
  try {
    const Queue = (await vite.ssrLoadModule('/src/views/QueueView.vue')).default
    const { useQueueStore } = await vite.ssrLoadModule('/src/stores/queue.ts')
    const template = parse(await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')).descriptor.template.content
    const render = new Function('Vue', compile(template, { mode: 'function', prefixIdentifiers: true }).code)(Vue)
    const pinia = createPinia(), store = useQueueStore(pinia)
    store.load = async () => {}
    store.startProgressTick = () => {}
    store.stopProgressTick = () => {}
    let state, mounts = 0
    const Tasks = { setup(props, context) {
      mounts++
      state = Queue.setup(props, context)
      return () => Vue.h('main', state.importOpen.value ? 'import-open' : 'import-closed')
    } }
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/tasks', name: 'tasks', component: Tasks },
      ...['/library', '/profile'].map(path => ({ path, component: { render: () => null } })),
    ] })
    await router.push('/tasks?openImport=1')
    const { root, host } = renderer()
    app = host.createApp({ render, components: { ToastHost: { render: () => null } } }).use(pinia).use(router)
    app.provide(Vue.ssrContextKey, { modules: new Set() })
    app.mount(root)
    await new Promise(resolve => setImmediate(resolve))
    await Vue.nextTick()
    assert.equal(state.importOpen.value, true)
    assert.deepEqual(router.currentRoute.value.query, {})
    state.importOpen.value = false
    await router.push('/tasks?openImport=1')
    await new Promise(resolve => setImmediate(resolve))
    await Vue.nextTick()
    assert.equal(state.importOpen.value, true)
    assert.equal(mounts, 1)
  } finally {
    app?.unmount()
    await vite.close()
    if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
    else delete globalThis.localStorage
  }
})
