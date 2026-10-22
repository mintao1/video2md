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
