import assert from 'node:assert/strict'
import { after, before, it } from 'node:test'
import { createServer } from 'vite'

let vite, pdf
before(async () => {
  vite = await createServer({ configFile: false, server: { middlewareMode: true } })
  pdf = await vite.ssrLoadModule('/src/pdf.ts')
})
after(async () => { await vite?.close() })

it('builds an A4 print document with safe title, print instructions, and MathJax SVG support', () => {
  const html = pdf.buildPdfHtml('代数 <教材>', '<section class="pdf-chapter"><h2>第一讲</h2><p>内容</p></section>')
  assert.match(html, /<title>代数 &lt;教材&gt; · PDF<\/title>/)
  assert.match(html, /保存为 PDF/)
  assert.match(html, /size: A4/)
  assert.match(html, /tex-svg\.js/)
  assert.match(html, /pdf-chapter/)
})

it('keeps the PDF action wired separately from the Markdown download path', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/views/LibraryView.vue', import.meta.url), 'utf8')
  const modal = await readFile(new URL('../src/components/book/ExportFormatModal.vue', import.meta.url), 'utf8')
  assert.match(source, /format === 'PDF'/)
  assert.match(source, /printNotesAsPdf\(book\.title, notes/)
  assert.match(source, /format !== 'Markdown'/)
  assert.match(modal, /PDF（打印保存）/)
})

it('uses the bundled MathJax runtime instead of a remote CDN', () => {
  const html = pdf.buildPdfHtml('离线公式', '<p>$$x^2$$</p>')
  assert.match(html, /\/vendor\/mathjax\/tex-svg\.js/)
  assert.doesNotMatch(html, /jsdelivr\.net\/npm\/mathjax/)
})

it('ships the self-contained MathJax asset with the frontend', async () => {
  const { stat } = await import('node:fs/promises')
  const asset = await stat(new URL('../public/vendor/mathjax/tex-svg.js', import.meta.url))
  assert.ok(asset.size > 1_000_000)
})

it('classifies an expired deliver artifact so export can explain the 410', async () => {
  const books = await vite.ssrLoadModule('/src/api/live/books.ts?expired-test')
  const previousFetch = globalThis.fetch
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
  globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: 410, message: 'expired' } }), {
    status: 410,
    headers: { 'content-type': 'application/json' },
  })
  try {
    await assert.rejects(books.getNotes('expired-run'), (error) =>
      error?.name === 'ArtifactsExpiredError' && error?.code === 'ARTIFACT_EXPIRED')
  } finally {
    globalThis.fetch = previousFetch
    globalThis.localStorage = previousStorage
  }
})

it('aborts a media request after the configured timeout', async () => {
  const client = await vite.ssrLoadModule('/src/api/client.ts?timeout-test')
  const previousFetch = globalThis.fetch
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
  globalThis.fetch = async (_input, init) => await new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
  })
  try {
    await assert.rejects(client.requestBlob('/slow-media', { timeoutMs: 5 }), (error) =>
      error?.name === 'ApiTimeoutError' && error?.code === 'REQUEST_TIMEOUT')
  } finally {
    globalThis.fetch = previousFetch
    globalThis.localStorage = previousStorage
  }
})
