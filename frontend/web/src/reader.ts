/** Convert an external chapter parameter to a safe zero-based index. */
export function chapterIndex(chapter: unknown, total: number, progress = 0): number {
  if (total <= 0) return 0
  const requested = typeof chapter === 'string' || typeof chapter === 'number' ? Number(chapter) : NaN
  const saved = Number.isFinite(progress) ? Math.round((progress / 100) * total) : 1
  const no = Number.isInteger(requested) && requested >= 1 ? requested : saved
  return Math.min(total - 1, Math.max(0, no - 1))
}

const EMBED = /!\[\[attachments\/([^\]\n\\/|]+)(?:\|[^\]]*)?\]\]/g

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
export function mediaMarkdown(md: string, urls: Record<string, string>): string {
  return md.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '').replace(EMBED, (_match, name: string) => {
    const url = urls[name]
    if (!url?.startsWith('blob:')) return `<span>媒体暂不可用：${escape(name)}</span>`
    const src = escape(url)
    return /\.mp4$/i.test(name)
      ? `<video src="${src}" controls preload="metadata" data-v2md-media="${escape(name)}" aria-label="${escape(name)}"></video>`
      : `<img src="${src}" data-v2md-media="${escape(name)}" alt="${escape(name)}">`
  })
}
