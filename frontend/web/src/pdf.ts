import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { mediaMarkdown, mediaNames } from './reader'

export type PdfNote = { title: string; md: string }
export type PdfMediaLoader = (name: string) => Promise<Blob>
type PdfCallout = { type: 'info' | 'tip' | 'warning' | 'example'; title: string; body: string }

const CALLOUT_START = /^>\s*\[!(info|tip|warning|example)\](?:-)?(?:\s+(.*))?\s*$/

function extractCallouts(md: string): { markdown: string; callouts: PdfCallout[] } {
  const lines = md.split(/\r?\n/)
  const output: string[] = []
  const callouts: PdfCallout[] = []
  for (let i = 0; i < lines.length; i++) {
    const match = CALLOUT_START.exec(lines[i] ?? '')
    if (!match) {
      output.push(lines[i] ?? '')
      continue
    }
    const body: string[] = []
    while (i + 1 < lines.length && /^>\s?/.test(lines[i + 1] ?? '')) {
      i += 1
      body.push((lines[i] ?? '').replace(/^>\s?/, ''))
    }
    callouts.push({
      type: match[1] as PdfCallout['type'],
      title: match[2]?.trim() || match[1],
      body: body.join('\n').trim(),
    })
    output.push(`<!--V2MD_CALLOUT_${callouts.length - 1}-->`)
  }
  return { markdown: output.join('\n'), callouts }
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[char] ?? char)
}

function renderNote(md: string, mediaUrls: Record<string, string>): string {
  const extracted = extractCallouts(md)
  let source = mediaMarkdown(extracted.markdown, mediaUrls)
  const calloutHtml = extracted.callouts.map((callout) => {
    const body = marked.parse(mediaMarkdown(callout.body, mediaUrls), { async: false }) as string
    return `<aside class="md-callout md-callout-${callout.type}"><h3>${escapeHtml(callout.title)}</h3><div>${body}</div></aside>`
  })
  extracted.callouts.forEach((_callout, index) => {
    source = source.replace(`<!--V2MD_CALLOUT_${index}-->`, calloutHtml[index] ?? '')
  })

  const fragment = DOMPurify.sanitize(marked.parse(source, { async: false }) as string, {
    RETURN_DOM_FRAGMENT: true,
  })
  // DOMPurify strips blob: URLs by design. Restore only URLs created from
  // authenticated media responses after all generated markup is sanitized.
  for (const element of fragment.querySelectorAll('img[data-v2md-media], video[data-v2md-media]')) {
    const name = element.getAttribute('data-v2md-media') ?? ''
    const url = mediaUrls[name]
    if (url?.startsWith('blob:')) element.setAttribute('src', url)
    element.removeAttribute('data-v2md-media')
  }
  const container = document.createElement('div')
  container.append(fragment)
  return container.innerHTML
}

function printStyles(): string {
  return `
    @page { size: A4; margin: 18mm 16mm 20mm; }
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body { margin: 0; color: #202124; background: #fff; font: 11pt/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", sans-serif; }
    .pdf-toolbar { padding: 12px 16px; background: #f5f5f5; color: #444; text-align: center; font-size: 12px; }
    .pdf-document { max-width: 820px; margin: 0 auto; }
    .pdf-cover { padding: 22mm 0 12mm; border-bottom: 2px solid #202124; }
    .pdf-cover h1 { margin: 0 0 8px; font-size: 28pt; line-height: 1.2; }
    .pdf-cover p { margin: 0; color: #666; }
    .pdf-chapter { padding-top: 10mm; break-inside: auto; }
    .pdf-chapter + .pdf-chapter { break-before: page; }
    .pdf-chapter > h2 { margin: 0 0 10mm; padding-bottom: 4px; border-bottom: 1px solid #aaa; font-size: 19pt; line-height: 1.25; }
    h1, h2, h3, h4 { color: #111; break-after: avoid; }
    h1 { font-size: 22pt; line-height: 1.25; }
    h2 { font-size: 17pt; line-height: 1.3; }
    h3 { font-size: 14pt; line-height: 1.35; }
    p { margin: 0.7em 0; orphans: 3; widows: 3; }
    ul, ol { padding-left: 1.5em; }
    li { margin: 0.25em 0; }
    blockquote { margin: 1em 0; padding: 0.15em 1em; border-left: 3px solid #888; color: #555; }
    pre { overflow-wrap: anywhere; white-space: pre-wrap; padding: 0.8em 1em; border-radius: 5px; background: #f1f3f4; font-size: 9pt; }
    code { padding: 0.1em 0.3em; border-radius: 3px; background: #f1f3f4; font-size: 0.9em; }
    pre code { padding: 0; background: transparent; }
    table { width: 100%; border-collapse: collapse; margin: 1em 0; font-size: 9.5pt; }
    th, td { padding: 5px 7px; border: 1px solid #999; text-align: left; vertical-align: top; }
    th { background: #eee; }
    a { color: #164e8a; text-decoration: underline; }
    img { display: block; max-width: 100%; max-height: 210mm; margin: 1em auto; break-inside: avoid; }
    video { display: none; }
    .md-callout { margin: 1em 0; padding: 0.2em 0.9em; border-left: 4px solid #777; background: #f7f7f7; break-inside: avoid; }
    .md-callout h3 { margin: 0.35em 0; font-size: 11pt; }
    .md-callout-info { border-left-color: #3f51b5; }
    .md-callout-tip { border-left-color: #008577; }
    .md-callout-warning { border-left-color: #b26a00; }
    .md-callout-example { border-left-color: #7b1fa2; }
    @media print {
      .pdf-toolbar { display: none; }
      .pdf-document { max-width: none; }
    }
  `
}

/** Build the printable document shell; kept pure so the bundle can test its contract. */
export function buildPdfHtml(title: string, chaptersHtml: string): string {
  const safeTitle = escapeHtml(title)
  return `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${safeTitle} · PDF</title>
    <style>${printStyles()}</style>
    <script>window.MathJax = { tex: { inlineMath: [['$','$'], ['\\(','\\)']], displayMath: [['$$','$$'], ['\\[','\\]']] }, svg: { fontCache: 'global' } }<\/script>
    <script async src="/vendor/mathjax/tex-svg.js"><\/script>
  </head>
  <body>
    <div class="pdf-toolbar">打印对话框中选择“保存为 PDF”即可导出文件</div>
    <main class="pdf-document">
      <header class="pdf-cover"><h1>${safeTitle}</h1><p>由视书生成 · ${new Date().toLocaleDateString('zh-CN')}</p></header>
      ${chaptersHtml}
    </main>
  </body>
</html>`
}

function waitForImages(popup: Window): Promise<void> {
  const images = Array.from(popup.document.images)
  return Promise.all(images.map((image) => {
    if (image.complete) return Promise.resolve()
    return new Promise<void>((resolve) => {
      image.addEventListener('load', () => resolve(), { once: true })
      image.addEventListener('error', () => resolve(), { once: true })
    })
  })).then(() => undefined)
}

async function waitForMathJax(popup: Window): Promise<void> {
  const deadline = Date.now() + 4000
  while (Date.now() < deadline) {
    const mathJax = (popup as Window & { MathJax?: { startup?: { promise?: Promise<unknown> } } }).MathJax
    if (mathJax?.startup?.promise) {
      await mathJax.startup.promise.catch(() => undefined)
      return
    }
    await new Promise((resolve) => window.setTimeout(resolve, 50))
  }
}

/** Open a browser print dialog with the authenticated notes rendered as a PDF-ready document. */
export async function printNotesAsPdf(
  title: string,
  notes: readonly PdfNote[],
  loadMedia: PdfMediaLoader,
  existingPopup?: Window | null,
): Promise<void> {
  if (typeof window === 'undefined') throw new Error('PDF 导出只能在浏览器中使用')
  const popup = existingPopup ?? window.open('', '_blank', 'popup,width=960,height=1200')
  if (!popup) throw new Error('浏览器阻止了打印窗口，请允许本站弹窗后重试')
  popup.opener = null

  const mediaUrls: Record<string, string> = {}
  const cleanup = () => Object.values(mediaUrls).forEach((url) => URL.revokeObjectURL(url))
  popup.addEventListener('beforeunload', cleanup, { once: true })

  try {
    const imageNames = [...new Set(notes.flatMap((note) => mediaNames(note.md)))]
      .filter((name) => !/\.mp4$/i.test(name))
    await Promise.all(imageNames.map(async (name) => {
      const blob = await loadMedia(name)
      mediaUrls[name] = URL.createObjectURL(blob)
    }))

    const chaptersHtml = notes.map((note, index) => `
      <section class="pdf-chapter">
        <h2>${index + 1}. ${escapeHtml(note.title || `第 ${index + 1} 讲`)}</h2>
        ${renderNote(note.md, mediaUrls)}
      </section>`).join('')
    popup.document.open()
    popup.document.write(buildPdfHtml(title, chaptersHtml))
    popup.document.close()
    await waitForImages(popup)
    await waitForMathJax(popup)
    popup.focus()
    popup.onafterprint = () => popup.close()
    popup.print()
  } catch (error) {
    cleanup()
    popup.close()
    throw error
  }
}
