import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const repo = fileURLToPath(new URL('../../..', import.meta.url))

test('profile pruning evaluation set is present and covers the requested hidden controls', async () => {
  const text = await readFile(`${repo}/docs/evals/profile-pruning-eval-set.jsonl`, 'utf8')
  const rows = text.trim().split('\n').map(line => JSON.parse(line))
  assert.deepEqual(rows.map(row => row.id), ['profile-pruning-001'])
  assert.ok(rows.every(row => row.input.actions.length > 0 && row.expectedOutput.criteria.length >= 5))
  assert.ok(rows.every(row => row.counterexamples.length >= 3))
  assert.ok(rows.every(row => row.metadata.offline_can_close_production === false))
})

test('profile source keeps the compact personal center contract', async () => {
  const view = await readFile(`${root}/src/views/ProfileView.vue`, 'utf8')
  const card = await readFile(`${root}/src/components/profile/ProfileCard.vue`, 'utf8')
  const header = await readFile(`${root}/src/components/common/AppHeader.vue`, 'utf8')
  const llm = await readFile(`${root}/src/components/profile/LlmSettingsCard.vue`, 'utf8')
  for (const marker of ['AI 转换时长', '教材库空间', '公式精准度', 'Notion', '立即升级', 'SVIP 旗舰学术特权', 'Pro 会员', 'SVIP 会员', '高级学术会员']) {
    assert.doesNotMatch(`${view}\n${card}\n${header}`, new RegExp(marker), `removed profile control returned: ${marker}`)
  }
  assert.match(card, /已生成教材/)
  assert.match(card, /提取知识点/)
  assert.match(llm, /LLM API 配置/)
})

test('new-user journey evaluation set records Colab, Kaggle, session and membership gates', async () => {
  const text = await readFile(`${repo}/docs/evals/new-user-login-journey-eval-set.jsonl`, 'utf8')
  const rows = text.trim().split('\n').map(line => JSON.parse(line))
  assert.deepEqual(rows.map(row => row.id), [
    'new-user-journey-001', 'new-user-journey-002', 'new-user-journey-003', 'new-user-journey-004',
  ])
  assert.ok(rows.every(row => row.input.actions.length >= 2 && row.expectedOutput.criteria.length >= 4))
  assert.ok(rows.every(row => row.counterexamples.length >= 2))
  assert.ok(rows.every(row => row.metadata.offline_can_close_production === false))
})
