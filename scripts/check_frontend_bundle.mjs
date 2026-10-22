#!/usr/bin/env node
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const dist = path.resolve(process.argv[2] ?? 'frontend/web/dist')
const htmlPath = path.join(dist, 'index.html')
const html = fs.readFileSync(htmlPath, 'utf8')
const scripts = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(match => match[1])
if (scripts.length === 0) throw new Error(`no module script in ${htmlPath}`)
const assetsDir = path.join(dist, 'assets')
const bundleFiles = fs.readdirSync(assetsDir).filter(file => file.endsWith('.js'))
const bundles = bundleFiles.map(file => fs.readFileSync(path.join(assetsDir, file), 'utf8')).join('\n')
for (const marker of ['https://www.kaggle.com/account/login', '登录 Kaggle']) {
  if (!bundles.includes(marker)) throw new Error(`built frontend is missing required marker: ${marker}`)
}
process.stdout.write(`frontend bundle gate passed: ${scripts.join(', ')}; scanned ${bundleFiles.length} JS assets\n`)
