// 队列页 live 实现（签名对齐 mock/impl.ts，index.ts 换源零改上层）。
// 映射口径：GET /runs 的 total/done_entries → progress；POST /runs 同步 resolve
// 建 run（RunCreatedOut 无 title/created，用 url 派生 + 当下时间，列表刷新后被服务端标题覆盖）。
import { request, requestBlob } from '../client'
import type { RunListItem, RunStatus } from '../../types'

interface RunListResp {
  items: {
    run_id: string
    url: string
    title?: string | null
    status?: string | null
    created?: string | null
    total?: number | null
    done_entries: number
    queue_pos?: number | null
    artifacts?: { present: boolean } | null
  }[]
  mode: string
}

interface RunCreatedResp {
  run_id: string
  total: number
  entries: { index: number; url: string; title?: string | null }[]
  all_skipped: boolean
  status: string
  dedup: boolean
}

/** 后端终态/partial/error 原样透传；queued/created 等直接是前端词表 */
function asStatus(s: string | null | undefined): RunStatus {
  const v = (s || 'created') as RunStatus
  return (['created', 'queued', 'running', 'paused', 'cancelling',
    'done', 'partial', 'error'] as string[]).includes(v) ? v : 'created'
}

function deriveTitle(url: string): string {
  try {
    const u = new URL(url)
    const seg = u.pathname.split('/').filter(Boolean).pop() || u.hostname
    return decodeURIComponent(seg).replace(/[-_+]/g, ' ')
  } catch {
    return '新转换任务'
  }
}

function sourceLabel(url: string): string | undefined {
  if (url.includes('bilibili')) return 'Bilibili'
  if (url.includes('youtube') || url.includes('youtu.be')) return 'YouTube'
  if (url.includes('douyin')) return '抖音'
  if (url.includes('xiaohongshu')) return '小红书'
  if (url.includes('pan.baidu.com')) return '百度网盘'
  return undefined
}

function toItem(i: RunListResp['items'][number], mode: string): RunListItem {
  const total = i.total ?? 0
  const done = i.done_entries ?? 0
  return {
    run_id: i.run_id,
    url: i.url,
    title: i.title || deriveTitle(i.url),
    status: asStatus(i.status),
    created: i.created || new Date().toISOString(),
    progress: total > 0 ? Math.round((done / total) * 100) : 0,
    queue_pos: i.queue_pos ?? undefined,
    source_label: sourceLabel(i.url),
    bundle_ready: mode === 'deliver' ? !!i.artifacts?.present : undefined,
  }
}

export async function listRuns(): Promise<RunListItem[]> {
  const resp = await request<RunListResp>('/runs')
  return resp.items.map((i) => toItem(i, resp.mode))
}

/** 验证档：只跑前 2 个条目（用户拍板「先跑 1-2 个验证」）；全量跑删掉这个字段即可 */
// 验证档只跑前 2 条（全量跑删掉该字段）。含 0：单视频条目 playlist_index=None，
// 后端 _part_selected 口径为 (index or 0)——spec 不含 0 会把单视频整条滤空（400）。
const VERIFY_PART_ITEMS = '0,1,2'

export async function createRun(body: { url: string; title?: string }): Promise<RunListItem> {
  // RunCreate 契约无 title 字段（展示名由服务端从 art/entry 派生），本地 title 仅兜底
  const created = await request<RunCreatedResp>('/runs', {
    method: 'POST',
    body: JSON.stringify({ url: body.url, part_items: VERIFY_PART_ITEMS }),
  })
  return {
    run_id: created.run_id,
    url: body.url,
    title: body.title || created.entries[0]?.title || deriveTitle(body.url),
    status: asStatus(created.status),
    created: new Date().toISOString(),
    progress: 0,
    queue_pos: created.status === 'queued' ? 1 : undefined,
    duration_label: '--:--',
    source_label: sourceLabel(body.url),
  }
}

export async function cancelRun(id: string): Promise<{ status: RunStatus }> {
  return request(`/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
}

export async function resumeRun(id: string): Promise<{ status: RunStatus }> {
  return request(`/runs/${encodeURIComponent(id)}/resume`, { method: 'POST' })
}

export async function promoteRun(id: string): Promise<void> {
  await request(`/runs/${encodeURIComponent(id)}/promote`, { method: 'POST' })
}

export async function deleteRuns(ids: string[]): Promise<void> {
  for (const id of ids) {
    await request(`/runs/${encodeURIComponent(id)}`, { method: 'DELETE' })
  }
}

export async function downloadBundle(id: string): Promise<Blob> {
  return requestBlob(`/runs/${encodeURIComponent(id)}/bundle`)
}
