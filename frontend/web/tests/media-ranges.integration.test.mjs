import assert from 'node:assert/strict'
import { before, after, afterEach, it } from 'node:test'
import { createServer } from 'vite'

let vite, client
const originalFetch = globalThis.fetch
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
before(async () => {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: () => 'integration-token', removeItem() {} },
  })
  vite = await createServer({ configFile: false, server: { middlewareMode: true, hmr: false } })
  client = await vite.ssrLoadModule('/src/api/client.ts')
})
afterEach(() => {
  globalThis.fetch = originalFetch
})
after(async () => {
  await vite?.close()
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else delete globalThis.localStorage
})

it('resumes at the failed range and reconstructs the exact authenticated video', async () => {
  const source = Uint8Array.from({ length: 150000 }, (_, i) => i % 251)
  const ranges = []
  globalThis.fetch = async (_url, init) => {
    assert.equal(init.headers.Authorization, 'Bearer integration-token')
    const [, startText, endText] = /^bytes=(\d+)-(\d+)$/.exec(init.headers.Range)
    const start = Number(startText),
      end = Math.min(Number(endText), source.length - 1)
    ranges.push(start)
    if (start > 0) assert.equal(init.headers['If-Range'], '"stable-file"')
    const body = ranges.length === 2 ? source.slice(start, start + 8) : source.slice(start, end + 1)
    return new Response(body, {
      status: 206,
      headers: {
        'Content-Type': 'video/mp4',
        'Content-Length': String(end - start + 1),
        'Content-Range': `bytes ${start}-${end}/${source.length}`,
        ETag: '"stable-file"',
      },
    })
  }
  const blob = await client.requestMediaBlob('/runs/example/media/clip.mp4')
  assert.equal(blob.type, 'video/mp4')
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), source)
  assert.deepEqual(ranges, [0, 65536, 65536, 131072])
})

it('rejects a changed file without mixing two versions', async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    const start = calls === 1 ? 0 : 65536,
      end = start + 65535
    return new Response(new Uint8Array(65536), {
      status: 206,
      headers: {
        'Content-Range': `bytes ${start}-${end}/150000`,
        ETag: `"version-${calls}"`,
      },
    })
  }
  await assert.rejects(client.requestMediaBlob('/media'), /分段响应不一致/)
  assert.equal(calls, 2)
})

it('bounds transport retries and preserves HTTP permission failures', async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    throw new TypeError('network interrupted')
  }
  await assert.rejects(client.requestMediaBlob('/media'), /媒体传输中断/)
  assert.equal(calls, 2)
  calls = 0
  globalThis.fetch = async () => {
    calls++
    return new Response('', { status: 403 })
  }
  await assert.rejects(client.requestMediaBlob('/media'), { code: 'HTTP_403' })
  assert.equal(calls, 1)
})

it('rejects an If-Range file change returned as a full 200 after the first chunk', async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    if (calls === 1)
      return new Response(new Uint8Array(65536), {
        status: 206,
        headers: {
          'Content-Range': 'bytes 0-65535/150000',
          ETag: '"original"',
        },
      })
    return new Response(new Uint8Array(150000), { status: 200, headers: { ETag: '"changed"' } })
  }
  await assert.rejects(client.requestMediaBlob('/media'), /分段响应不一致/)
  assert.equal(calls, 2)
})
