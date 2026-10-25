/** Convert an external chapter parameter to a safe zero-based index. */
export function chapterIndex(chapter: unknown, total: number, progress = 0): number {
  if (total <= 0) return 0
  const requested = typeof chapter === 'string' || typeof chapter === 'number' ? Number(chapter) : NaN
  const saved = Number.isFinite(progress) ? Math.round((progress / 100) * total) : 1
  const no = Number.isInteger(requested) && requested >= 1 ? requested : saved
  return Math.min(total - 1, Math.max(0, no - 1))
}

const EMBED = /!\[\[attachments\/([^\]\n\\/|]+)(?:\|[^\]]*)?\]\]/g
const CALLOUT_START = /^>\s*\[!(info|tip|warning|example)\](-)?(?:\s+(.*))?\s*$/

export type Callout = { type: 'info' | 'tip' | 'warning' | 'example'; fold: boolean; title: string; body: string }

/** Extract Obsidian blockquote callouts before marked parses them as plain text. */
export function extractCallouts(md: string): { markdown: string; callouts: Callout[] } {
  const lines = md.split(/\r?\n/)
  const output: string[] = []
  const callouts: Callout[] = []
  for (let i = 0; i < lines.length; i++) {
    const match = CALLOUT_START.exec(lines[i] ?? '')
    if (!match) {
      output.push(lines[i] ?? '')
      continue
    }
    const body: string[] = []
    while (i + 1 < lines.length) {
      const next = lines[i + 1] ?? ''
      if (!/^>\s?/.test(next)) break
      i += 1
      body.push(next.replace(/^>\s?/, ''))
    }
    const type = match[1] as Callout['type']
    callouts.push({ type, fold: Boolean(match[2]), title: match[3]?.trim() || type, body: body.join('\n').trim() })
    output.push(`<!--V2MD_CALLOUT_${callouts.length - 1}-->`)
  }
  return { markdown: output.join('\n'), callouts }
}

export function mediaNames(md: string): string[] {
  return [...new Set([...md.matchAll(EMBED)].map((m) => m[1]!))]
    .filter((name) => /\.(jpe?g|png|webp|mp4)$/i.test(name))
}

function escape(value: string): string {
  return value.replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]!)
}

/** URLs come exclusively from authenticated fetches, never from note text. */
export function mediaMarkdown(md: string, urls: Record<string, string>, pending: readonly string[] = []): string {
  return md.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '').replace(EMBED, (_match, name: string) => {
    const url = urls[name]
    if (!url?.startsWith('blob:')) return pending.includes(name)
      ? `<span role="status">媒体加载中：${escape(name)}</span>`
      : `<span>媒体暂不可用：${escape(name)}</span>`
    const src = escape(url)
    return /\.mp4$/i.test(name)
      ? `<video src="${src}" controls preload="metadata" data-v2md-media="${escape(name)}" aria-label="${escape(name)}"></video>`
      : `<img src="${src}" data-v2md-media="${escape(name)}" alt="${escape(name)}">`
  })
}

/** Publish each authenticated attachment independently; release URLs on chapter changes. */
export function startMediaLoad(
  names: readonly string[],
  fetchMedia: (name: string) => Promise<Blob>,
  onResult: (name: string, url: string | null) => void,
): () => void {
  let stopped = false
  const urls: string[] = []
  const fetchAttachment = async (name: string) => {
    try { return await fetchMedia(name) }
    catch (error) {
      // Fetch/body transport failures are TypeError. Retry the read once; HTTP
      // authorization/not-found errors must retain their original semantics.
      if (stopped || !(error instanceof TypeError)) throw error
      return fetchMedia(name)
    }
  }
  for (const name of names) {
    void fetchAttachment(name).then((blob) => {
      if (stopped) return
      const url = URL.createObjectURL(blob)
      urls.push(url)
      onResult(name, url)
    }, () => {
      if (!stopped) onResult(name, null)
    })
  }
  return () => {
    stopped = true
    for (const url of urls) URL.revokeObjectURL(url)
    urls.length = 0
  }
}
