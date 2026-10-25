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

it('extracts Obsidian callouts into stable typed blocks without losing body markdown', () => {
  const result = reader.extractCallouts('> [!info] 视频信息\n> - 来源：测试\n\n> [!tip]- 章节导航\n> - [00:00] 开始')
  assert.equal(result.callouts.length, 2)
  assert.deepEqual(result.callouts.map(c => [c.type, c.fold, c.title]), [
    ['info', false, '视频信息'], ['tip', true, '章节导航'],
  ])
  assert.match(result.callouts[0].body, /来源：测试/)
  assert.match(result.markdown, /V2MD_CALLOUT_0/)
  assert.doesNotMatch(result.markdown, /\[!info\]|\[!tip\]/)
})

it('escapes unavailable media names and refuses arbitrary external URLs', () => {
  const html = marked.parse(reader.mediaMarkdown('![[attachments/a" onerror="evil.jpg]]', {
    'a" onerror="evil.jpg': 'https://attacker.test/tracker.jpg',
  }))
  assert.doesNotMatch(html, /<img|<video|attacker\.test/)
  assert.match(html, /&quot;/)
})

it('shows a ready image while a slower clip is still loading, then reports a failed clip', async () => {
  const md = '![[attachments/frame.jpg]]\n\n![[attachments/clip.mp4]]'
  const pending = new Set(reader.mediaNames(md))
  const urls = {}
  const requests = {}
  const stop = reader.startMediaLoad([...pending], name => new Promise((resolve, reject) => {
    requests[name] = { resolve, reject }
  }), (name, url) => {
    if (url) urls[name] = url
    pending.delete(name)
  })
  const html = () => reader.mediaMarkdown(md, urls, [...pending])
  assert.doesNotMatch(html(), /暂不可用/)
  assert.match(html(), /role="status">媒体加载中：frame.jpg/)
  requests['frame.jpg'].resolve(new Blob(['image']))
  await Promise.resolve()
  await Promise.resolve()
  assert.match(html(), /<img src="blob:/)
  assert.match(html(), /媒体加载中：clip.mp4/)
  requests['clip.mp4'].reject(new Error('404'))
  await Promise.resolve()
  await Promise.resolve()
  assert.match(html(), /媒体暂不可用：clip.mp4/)
  assert.doesNotMatch(html(), /媒体加载中/)
  stop()
})

it('releases loaded URLs and ignores late media after leaving a chapter', async () => {
  let resolveLate
  const results = []
  const stop = reader.startMediaLoad(['frame.jpg', 'late.mp4'], name => name === 'frame.jpg'
    ? Promise.resolve(new Blob(['image']))
    : new Promise(resolve => { resolveLate = resolve }), (name, url) => results.push([name, url]))
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(results.length, 1)
  const url = results[0][1]
  assert.equal((await fetch(url)).status, 200)
  stop()
  resolveLate(new Blob(['late']))
  await Promise.resolve()
  assert.equal(results.length, 1)
  await assert.rejects(fetch(url))
})

it('retries a broken media transfer once without retrying HTTP rejections', async () => {
  const calls = {}, results = []
  const stop = reader.startMediaLoad(['transient.jpg', 'offline.mp4', 'forbidden.jpg'], async name => {
    calls[name] = (calls[name] || 0) + 1
    if (name === 'forbidden.jpg') throw new Error('HTTP_403')
    if (name === 'offline.mp4' || calls[name] === 1) throw new TypeError('Failed to fetch')
    return new Blob(['image'])
  }, (name, url) => results.push([name, url]))
  for (let i = 0; i < 8; i++) await Promise.resolve()
  assert.deepEqual(calls, { 'transient.jpg': 2, 'offline.mp4': 2, 'forbidden.jpg': 1 })
  assert.equal(results.length, 3)
  assert.match(results.find(([name]) => name === 'transient.jpg')[1], /^blob:/)
  assert.equal(results.find(([name]) => name === 'offline.mp4')[1], null)
  assert.equal(results.find(([name]) => name === 'forbidden.jpg')[1], null)
  stop()
})
