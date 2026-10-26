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
