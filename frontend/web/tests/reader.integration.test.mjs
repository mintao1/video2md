import assert from 'node:assert/strict'
import { after, before, it } from 'node:test'
import { createServer } from 'vite'
import { marked } from 'marked'

let vite, reader
before(async () => {
  vite = await createServer({ configFile: false, server: { middlewareMode: true } })
  reader = await vite.ssrLoadModule('/src/reader.ts')
})
after(async () => { await vite?.close() })

it('keeps invalid and fractional chapter parameters inside the note array', () => {
  for (const query of ['1.5', 'NaN', 'Infinity', '-1', ['2'], undefined]) {
    assert.equal(reader.chapterIndex(query, 3, 67), 1)
  }
  assert.equal(reader.chapterIndex('100', 3), 2)
  assert.equal(reader.chapterIndex('1', 3), 0)
  assert.equal(reader.chapterIndex('2', 0), 0)
})

it('renders Obsidian image and video references as actual media elements', () => {
  const md = '---\ntitle: Test\n---\n![[attachments/frame.jpg]]\n\n![[attachments/clip.mp4]]'
  assert.deepEqual(reader.mediaNames(md), ['frame.jpg', 'clip.mp4'])
  const html = marked.parse(reader.mediaMarkdown(md, {
    'frame.jpg': 'blob:frame', 'clip.mp4': 'blob:clip',
  }))
  assert.match(html, /<img src="blob:frame"/)
  assert.match(html, /<video src="blob:clip" controls/)
  assert.doesNotMatch(html, /!\[\[|title: Test/)
})

it('escapes unavailable media names and refuses arbitrary external URLs', () => {
  const html = marked.parse(reader.mediaMarkdown('![[attachments/a" onerror="evil.jpg]]', {
    'a" onerror="evil.jpg': 'https://attacker.test/tracker.jpg',
  }))
  assert.doesNotMatch(html, /<img|<video|attacker\.test/)
  assert.match(html, /&quot;/)
})
