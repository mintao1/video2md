// Deterministic checkers for docs/evals/frontend-quality-eval-set.jsonl.
// Each checker reads files through `repo` (a virtual view of the repository that
// counterexample patches can modify) and returns a list of violations.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, normalize, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

export const repoRoot = fileURLToPath(new URL('../../..', import.meta.url))
const WEB = 'frontend/web'
const READMES = ['README.md', `${WEB}/README.md`]
const SCREENSHOTS = ['brand', 'library', 'tasks', 'reader', 'profile'].map((n) => `docs/screenshots/${n}.png`)
const PNG_SIGNATURE = '\x89PNG\r\n\x1a\n'

/** Text-file view of the repo with in-memory overrides (string) and deletions (null). */
export class Repo {
  constructor(overrides = new Map()) {
    this.overrides = overrides
  }
  exists(path) {
    if (this.overrides.has(path)) return this.overrides.get(path) !== null
    return existsSync(join(repoRoot, path))
  }
  read(path) {
    if (this.overrides.has(path)) {
      const value = this.overrides.get(path)
      if (value === null) throw new Error(`missing file: ${path}`)
      return value
    }
    return readFileSync(join(repoRoot, path), 'utf8')
  }
  readBinaryPrefix(path, length) {
    if (this.overrides.has(path)) return (this.overrides.get(path) ?? '').slice(0, length)
    return readFileSync(join(repoRoot, path)).subarray(0, length).toString('latin1')
  }
  list(dir, pattern) {
    const out = []
    const walk = (rel) => {
      const abs = join(repoRoot, rel)
      if (!existsSync(abs)) return
      for (const name of readdirSync(abs)) {
        const child = `${rel}/${name}`
        if (statSync(join(repoRoot, child)).isDirectory()) walk(child)
        else if (pattern.test(child)) out.push(child)
      }
    }
    walk(dir)
    for (const [path, value] of this.overrides) {
      if (path.startsWith(`${dir}/`) && pattern.test(path) && value !== null && !out.includes(path)) out.push(path)
    }
    return out.filter((path) => this.exists(path)).sort()
  }
  /** Apply one counterexample patch, returning a new view. */
  patched(patch) {
    const overrides = new Map(this.overrides)
    const view = new Repo(overrides)
    if (patch.delete) overrides.set(patch.file, null)
    else if (patch.create !== undefined) overrides.set(patch.file, patch.create)
    else {
      const text = view.read(patch.file)
      if (patch.append !== undefined) overrides.set(patch.file, text + patch.append)
      else {
        if (!text.includes(patch.find)) throw new Error(`stale counterexample: "${patch.find}" not in ${patch.file}`)
        overrides.set(patch.file, text.replace(patch.find, patch.replace))
      }
    }
    return view
  }
}

function codeBlocks(markdown) {
  return [...markdown.matchAll(/```[^\n]*\n([\s\S]*?)```/g)].map((m) => m[1]).join('\n')
}

export const checks = {
  /** Relative links, images and `src/...` paths in both READMEs resolve to real files. */
  readme_paths_exist(repo) {
    const violations = []
    for (const readme of READMES) {
      const text = repo.read(readme)
      const base = dirname(readme)
      for (const [, target] of text.matchAll(/!?\[[^\]]*\]\(([^)\s#]+)(?:#[^)]*)?\)/g)) {
        if (/^[a-z]+:/i.test(target)) continue
        const path = normalize(join(base, target))
        if (!repo.exists(path)) violations.push(`${readme}: link target missing: ${target}`)
      }
      if (readme.startsWith(WEB)) {
        for (const [, path] of text.matchAll(/`(src\/[\w./-]+\.(?:ts|vue|mjs))`/g)) {
          if (!repo.exists(`${WEB}/${path}`)) violations.push(`${readme}: code path missing: ${path}`)
        }
      }
    }
    return violations
  },

  /** Install instructions use the lockfile (`npm ci`) everywhere. */
  readme_install_uses_npm_ci(repo) {
    const violations = []
    for (const readme of READMES) {
      const blocks = codeBlocks(repo.read(readme))
      if (/\bnpm (?:install|i)\b/.test(blocks)) violations.push(`${readme}: uses npm install instead of npm ci`)
      if (readme.startsWith(WEB) && !/\bnpm ci\b/.test(blocks)) violations.push(`${readme}: missing npm ci`)
    }
    return violations
  },

  /** Every npm script is documented as `npm run <name>` in the frontend README. */
  readme_scripts_documented(repo) {
    const scripts = Object.keys(JSON.parse(repo.read(`${WEB}/package.json`)).scripts ?? {})
    const blocks = codeBlocks(repo.read(`${WEB}/README.md`))
    return scripts
      .filter((name) => !new RegExp(`npm run ${name.replace(/[:]/g, '\\$&')}(?![\\w:-])`).test(blocks))
      .map((name) => `${WEB}/README.md: script not documented: ${name}`)
  },

  /** Both READMEs embed every screenshot, and each screenshot is a PNG. */
  readme_screenshots(repo) {
    const violations = []
    for (const shot of SCREENSHOTS) {
      if (!repo.exists(shot)) violations.push(`missing screenshot: ${shot}`)
      else if (repo.readBinaryPrefix(shot, 8) !== PNG_SIGNATURE) violations.push(`not a PNG: ${shot}`)
      for (const readme of READMES) {
        const rel = relative(dirname(readme), shot)
        if (!repo.read(readme).includes(`](${rel})`)) violations.push(`${readme}: does not embed ${rel}`)
      }
    }
    return violations
  },

  /** The frontend README no longer describes a mock API layer. */
  readme_no_stale_mock(repo) {
    const text = repo.read(`${WEB}/README.md`)
    const violations = []
    if (/mock/i.test(text)) violations.push(`${WEB}/README.md: mentions mock`)
    if (/fetch 占位|第一版/.test(text)) violations.push(`${WEB}/README.md: describes the client as a placeholder`)
    return violations
  },

  /** No user-visible prototype/mock wording in application source. */
  no_mock_text_in_src(repo) {
    const banned =
      /mock|模拟支付|模拟|u_mock|草稿已保存|已复制教材分享链接|已完成云端同步|正在封装并打包|fetch 封装占位/i
    return repo.list(`${WEB}/src`, /\.(?:vue|ts)$/).flatMap((path) =>
      repo
        .read(path)
        .split('\n')
        .map((line, i) => (banned.test(line) ? `${path}:${i + 1}: ${line.trim()}` : null))
        .filter(Boolean),
    )
  },

  /** Removed fake features stay removed. */
  no_fake_features(repo) {
    const violations = []
    for (const file of ['VipMembershipSheet.vue', 'SvipPromoBanner.vue']) {
      if (repo.exists(`${WEB}/src/components/profile/${file}`)) violations.push(`fake feature restored: ${file}`)
    }
    if (repo.exists(`${WEB}/src/components/book/ExportFormatModal.vue`)) {
      violations.push('fake feature restored: ExportFormatModal.vue')
    }
    const modal = repo.read(`${WEB}/src/components/queue/ImportModal.vue`)
    if (/'draft'|saveDraft/.test(modal)) violations.push('ImportModal: draft action has no backend')
    if (/source\s*===|'local'|'cloud'/.test(modal)) violations.push('ImportModal: cloud/local sources have no backend')
    if (/ocrEnabled|tocEnabled|template\s*=/.test(modal))
      violations.push('ImportModal: options are not sent to the API')
    for (const path of repo.list(`${WEB}/src`, /\.(?:vue|ts)$/)) {
      const text = repo.read(path)
      if (/notionSync|v2md_notion/i.test(text)) violations.push(`${path}: Notion sync has no backend`)
      if (/\(e: 'share'|emit\('share'|@share=/.test(text)) violations.push(`${path}: share action has no backend`)
      if (/@pay=|'pay'/.test(text)) violations.push(`${path}: payment has no backend`)
    }
    return violations
  },

  /** Download buttons only appear when the backend reports a bundle. */
  download_gated_by_bundle(repo) {
    const violations = []
    const gates = {
      [`${WEB}/src/components/book/BookCard.vue`]: /v-if="book\.bundle_ready"[\s\S]{0,400}emit\('download', book\)/,
      [`${WEB}/src/components/queue/TaskCard.vue`]: /v-if="run\.bundle_ready"[\s\S]{0,400}action\('download'\)/,
    }
    for (const [path, pattern] of Object.entries(gates)) {
      if (!pattern.test(repo.read(path))) violations.push(`${path}: download not gated by bundle_ready`)
    }
    const books = repo.read(`${WEB}/src/api/live/books.ts`)
    if (!/bundle_ready:\s*mode === 'deliver' && !!r\.artifacts\?\.present/.test(books)) {
      violations.push('books.ts: bundle_ready must require deliver mode and present artifacts')
    }
    return violations
  },

  /** Every E2E flow asserts that no mock wording is visible. */
  e2e_asserts_no_mock_text(repo) {
    const violations = []
    const fixtures = repo.read(`${WEB}/e2e/fixtures.ts`)
    if (!/export const MOCK_TEXT = \/[^/]*mock[^/]*\/i/.test(fixtures))
      violations.push('fixtures.ts: MOCK_TEXT missing')
    for (const spec of ['login', 'submit', 'read']) {
      const text = repo.read(`${WEB}/e2e/${spec}.spec.ts`)
      if (!text.includes('await expectNoMockText(page)')) violations.push(`${spec}.spec.ts: no mock-text assertion`)
    }
    return violations
  },

  /** Lint/format scripts are strict and dev dependencies are pinned exactly. */
  lint_config_strict(repo) {
    const violations = []
    const pkg = JSON.parse(repo.read(`${WEB}/package.json`))
    if (!/^eslint \. --max-warnings 0$/.test(pkg.scripts?.lint ?? '')) violations.push('lint must fail on warnings')
    if (pkg.scripts?.['format:check'] !== 'prettier --check .') violations.push('format:check script missing')
    for (const [name, version] of Object.entries({ ...pkg.dependencies, ...pkg.devDependencies })) {
      if (!/^\d+\.\d+\.\d+$/.test(version)) violations.push(`${name}@${version} is not pinned exactly`)
    }
    const config = repo.read(`${WEB}/eslint.config.js`)
    for (const piece of ["vue.configs['flat/recommended']", 'tseslint.configs.recommended', 'js.configs.recommended']) {
      if (!config.includes(piece)) violations.push(`eslint.config.js: missing ${piece}`)
    }
    if (!/\n {2}prettier,\n\)\s*$/.test(config)) violations.push('eslint.config.js: prettier must be the last entry')
    if (!/'vue\/no-v-html': 'error'/.test(config)) violations.push("eslint.config.js: 'vue/no-v-html' must be 'error'")
    if (!repo.exists(`${WEB}/.prettierrc.json`)) violations.push('.prettierrc.json missing')
    return violations
  },

  /** Every v-html sink carries a justified ESLint exemption. */
  v_html_justified(repo) {
    const violations = []
    for (const path of repo.list(`${WEB}/src`, /\.vue$/)) {
      const lines = repo.read(path).split('\n')
      lines.forEach((line, i) => {
        if (!/\sv-html=/.test(line)) return
        const window = lines.slice(Math.max(0, i - 6), i).join('\n')
        if (!/eslint-disable-next-line vue\/no-v-html -- \S/.test(window)) {
          violations.push(`${path}:${i + 1}: v-html without a justified exemption`)
        }
      })
    }
    return violations
  },

  /** CI runs every frontend quality gate in the web job. */
  ci_runs_frontend_gates(repo) {
    const ci = repo.read('.github/workflows/ci.yml')
    const job = ci.slice(ci.indexOf('  web-integration:'), ci.indexOf('\n  docker-build:'))
    const order = [
      'npm ci',
      'npm run lint',
      'npm run format:check',
      'npm run build',
      'npm run test:integration',
      'npm run test:regression',
      'npx playwright install --with-deps chromium',
      'npm run test:e2e',
    ]
    const violations = []
    let cursor = 0
    for (const step of order) {
      const at = job.indexOf(`- run: ${step}\n`, cursor)
      if (at < 0) violations.push(`ci.yml web-integration: missing or out-of-order step: ${step}`)
      else cursor = at
    }
    return violations
  },

  /** E2E covers login, submission and reading against the real fixture backend. */
  e2e_flows_covered(repo) {
    const required = {
      login: ['#id_token=', "getByRole('checkbox').check()", '稍后连接，继续使用', 'reason=expired', '登录已过期'],
      submit: [
        "request().method() === 'POST'",
        '__tests/budget',
        "toHaveText('请输入视频链接')",
        'toHaveCount(before)',
      ],
      read: ["toHaveAttribute('src', /^blob:/)", 'naturalWidth', 'page.reload()', '进度 100%', '下载 Markdown 原始包'],
    }
    const violations = []
    for (const [spec, needles] of Object.entries(required)) {
      const path = `${WEB}/e2e/${spec}.spec.ts`
      if (!repo.exists(path)) {
        violations.push(`missing ${path}`)
        continue
      }
      const text = repo.read(path)
      if (/test\.(?:skip|only|fixme)\(/.test(text)) violations.push(`${path}: skipped or focused test`)
      for (const needle of needles) if (!text.includes(needle)) violations.push(`${path}: missing ${needle}`)
    }
    return violations
  },

  /** The real ESLint config reports each probe's rule (lint is not a no-op). */
  async eslint_probes(repo, input) {
    const { ESLint } = await import('eslint')
    const eslint = new ESLint({ cwd: join(repoRoot, WEB) })
    const violations = []
    for (const probe of input.probes) {
      const [result] = await eslint.lintText(probe.text, { filePath: join(repoRoot, WEB, probe.file) })
      const rules = result.messages.map((m) => m.ruleId)
      if (!rules.includes(probe.rule))
        violations.push(`${probe.file}: ESLint did not report ${probe.rule} (got ${rules})`)
    }
    return violations
  },

  /** The real Prettier config rejects unformatted probes. */
  async prettier_probes(repo, input) {
    const prettier = await import('prettier')
    const violations = []
    for (const probe of input.probes) {
      const filepath = join(repoRoot, WEB, probe.file)
      const options = { ...(await prettier.resolveConfig(filepath)), filepath }
      if (await prettier.check(probe.text, options))
        violations.push(`${probe.file}: Prettier accepted unformatted code`)
    }
    return violations
  },

  /** E2E talks to the real FastAPI fixture; only Google's page is intercepted. */
  e2e_real_backend(repo) {
    const violations = []
    const config = repo.read(`${WEB}/playwright.config.ts`)
    if (!/command: `[^`]*backend\/tests\/fixtures\/web_integration_api\.py/.test(config)) {
      violations.push('webServer is not the API fixture')
    }
    if (!/forbidOnly: Boolean\(process\.env\.CI\)/.test(config)) violations.push('forbidOnly must be enabled on CI')
    if (/retries: [1-9]/.test(config)) violations.push('retries would hide flaky failures')
    for (const path of repo.list(`${WEB}/e2e`, /\.ts$/)) {
      for (const [, pattern] of repo.read(path).matchAll(/page\.route\(\s*'([^']+)'/g)) {
        if (pattern !== '**/auth/google/login**') violations.push(`${path}: intercepts ${pattern}`)
      }
    }
    return violations
  },
}
