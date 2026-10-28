import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const repo = fileURLToPath(new URL('../../..', import.meta.url))

test('frontend production button evaluation set is present', async () => {
  const rows = (await readFile(`${repo}/docs/evals/frontend-production-buttons-eval-set.jsonl`, 'utf8'))
    .trim().split('\n').map(line => JSON.parse(line))
  assert.deepEqual(rows.map(row => row.id), ['frontend-buttons-001'])
  assert.ok(rows[0].expectedOutput.criteria.length >= 5)
  assert.ok(rows[0].counterexamples.length >= 4)
  assert.equal(rows[0].metadata.offline_can_close_production, false)
})

test('production button source has no mock fallbacks', async () => {
  const [brand, modal, queue, profile, uploads] = await Promise.all([
    readFile(`${root}/src/views/BrandView.vue`, 'utf8'),
    readFile(`${root}/src/components/queue/ImportModal.vue`, 'utf8'),
    readFile(`${root}/src/views/QueueView.vue`, 'utf8'),
    readFile(`${root}/src/views/ProfileView.vue`, 'utf8'),
    readFile(`${root}/src/api/live/uploads.ts`, 'utf8'),
  ])
  assert.match(brand, /:disabled="busy \|\| !agreed"/)
  assert.match(brand, /terms_accepted: '1'/)
  assert.match(modal, /type="file"/)
  assert.match(modal, /v2pdf_import_draft/)
  assert.match(modal, /source === 'link' \|\| source === 'cloud'/)
  assert.match(modal, /if \(next !== 'local'\) file\.value = null/)
  assert.doesNotMatch(modal, /BV1WE411d7v4/)
  assert.doesNotMatch(`${modal}\n${queue}`, /mock/i)
  assert.match(queue, /uploadFile\(payload\.file\)/)
  assert.match(uploads, /FormData/)
  assert.doesNotMatch(profile, /u_mock|ID: ----|视书用户/)
})
