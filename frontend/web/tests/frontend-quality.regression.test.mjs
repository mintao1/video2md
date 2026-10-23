// Executes docs/evals/frontend-quality-eval-set.jsonl: every case must pass on the
// repository as it is, and every counterexample must make its checker fail.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it } from 'node:test'
import { checks, Repo, repoRoot } from './quality-checks.mjs'

const EVAL_SET = 'docs/evals/frontend-quality-eval-set.jsonl'
const cases = readFileSync(join(repoRoot, EVAL_SET), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map((line) => JSON.parse(line))

describe('eval set integrity', () => {
  it('has unique ids, known checks and at least three counterexamples per case', () => {
    const ids = new Set()
    for (const c of cases) {
      assert.ok(!ids.has(c.id), `duplicate id ${c.id}`)
      ids.add(c.id)
      assert.ok(checks[c.input.check], `${c.id}: unknown check ${c.input.check}`)
      assert.deepEqual(c.expectedOutput.violations, [], `${c.id}: expected output must be zero violations`)
      assert.ok(c.expectedOutput.criteria.length > 0, `${c.id}: criteria missing`)
      assert.ok(c.metadata.counterexamples.length >= 3, `${c.id}: needs >= 3 counterexamples`)
    }
  })

  it('covers all four remediation items', () => {
    const items = new Set(cases.map((c) => c.metadata.item))
    assert.deepEqual([...items].sort(), ['e2e', 'lint', 'mock', 'readme'])
  })
})

const repo = new Repo()
for (const c of cases) {
  describe(`${c.id} (${c.metadata.item}): ${c.input.scenario}`, () => {
    const check = checks[c.input.check]

    it('passes on the current repository', async () => {
      assert.deepEqual(await check(repo, c.input), c.expectedOutput.violations)
    })

    for (const counter of c.metadata.counterexamples) {
      it(`rejects counterexample: ${counter.description}`, async () => {
        const view = counter.patch ? repo.patched(counter.patch) : repo
        const input = counter.probes ? { ...c.input, probes: counter.probes } : c.input
        const violations = await check(view, input)
        assert.ok(violations.length > 0, `${c.id}: counterexample was not detected: ${counter.description}`)
      })
    }
  })
}
