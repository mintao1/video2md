#!/usr/bin/env node
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const dist = path.resolve(process.argv[2] ?? 'frontend/web/dist')
const htmlPath = path.join(dist, 'index.html')
const html = fs.readFileSync(htmlPath, 'utf8')
const scripts = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(match => match[1])
if (scripts.length === 0) throw new Error(`no module script in ${htmlPath}`)
// Only scan JS the published index.html actually loads (entry plus its static/dynamic imports),
// so a stale entry next to a fresh bundle in assets/ cannot pass the gate.
const pending = scripts.map(src => path.join(dist, src.replace(/^\//, '')))
const seen = new Set()
const sources = []
while (pending.length) {
  const file = path.resolve(pending.pop())
  if (seen.has(file) || !file.startsWith(dist + path.sep) || !fs.existsSync(file)) continue
  seen.add(file)
  const source = fs.readFileSync(file, 'utf8')
  sources.push(source)
  for (const [, spec] of source.matchAll(/["'`](\.{1,2}\/[^"'`]+\.js)["'`]/g)) {
    pending.push(path.join(path.dirname(file), spec))
  }
}
if (sources.length === 0) throw new Error(`module scripts referenced by ${htmlPath} are missing`)
const bundles = sources.join('\n')
for (const marker of ['https://www.kaggle.com/account/login', '登录 Kaggle']) {
  if (!bundles.includes(marker)) throw new Error(`built frontend is missing required marker: ${marker}`)
}
process.stdout.write(`frontend bundle gate passed: ${scripts.join(', ')}; scanned ${sources.length} reachable JS assets\n`)
