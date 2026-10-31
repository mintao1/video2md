import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))

test('queue submission failures stay visible and preserve the form', async () => {
  const queue = await readFile(`${root}/src/views/QueueView.vue`, 'utf8')
  const modal = await readFile(`${root}/src/components/queue/ImportModal.vue`, 'utf8')

  assert.match(queue, /function submissionMessage[\s\S]*提交失败/)
  assert.equal((queue.match(/catch \(error\)[\s\S]{0,80}submissionMessage\(error\)/g) || []).length, 2)
  assert.match(queue, /await queue\.createRun[\s\S]*importOpen\.value = false/)
  const startFunction = modal.slice(modal.indexOf('function start()'), modal.indexOf('function saveDraft()'))
  assert.doesNotMatch(startFunction, /emit\('update:modelValue', false\)/)
})

test('playlist entry selection is optional and reaches the run submission', async () => {
  const queue = await readFile(`${root}/src/views/QueueView.vue`, 'utf8')
  const modal = await readFile(`${root}/src/components/queue/ImportModal.vue`, 'utf8')

  assert.match(modal, /合集条目（可选）/)
  assert.match(modal, /留空处理全部，如 1,3-5/)
  assert.match(modal, /partItems: source\.value === 'local' \? undefined : selected \|\| undefined/)
  assert.match(modal, /\^\\d\+\(\?:-\\d\+\)\?\(\?:,\\d\+\(\?:-\\d\+\)\?\)\*\$/)
  assert.match(queue, /queue\.createRun\(url, payload\.title, payload\.partItems\)/)
})
