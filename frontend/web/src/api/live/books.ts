// 教材库生产 API 实现。
// 映射口径：一本「书」= 一条 done/partial 的 run；收藏/阅读进度来自 /prefs（后端入库）。
// 页面/页码等展示字段使用真实可得数据（讲数、状态徽章），不造假。
import { ApiRequestError, request, requestBlob, requestMediaBlob } from '../client'
import type { Book, RunStatus, TocChapter } from '../../types'

/** GET /runs 列表项（后端 RunListOut；title 为服务端三路派生） */
interface RunListResp {
  items: {
    run_id: string
    url: string
    title?: string | null
    status: RunStatus
    created?: string | null
    total?: number | null
    done_entries: number
    artifacts?: { present?: boolean } | null
  }[]
  mode: string
}

interface PrefsResp {
  items: { run_id: string; fav: boolean; progress: number }[]
}

interface RunNotesResp {
  notes: { title: string; path: string; md: string }[]
}

interface RunDetailResp {
  entries?: {
    index: number
    url: string
    skipped?: boolean
    note?: string | null
    title?: string | null
  }[]
}

/** 模块级偏好缓存：toggleFav 只拿 run_id，当前值从这里读（getBooks 时重建） */
let prefsCache = new Map<string, { fav: boolean; progress: number }>()

const HOST_BADGE: Record<string, string> = {
  'www.bilibili.com': 'B站',
  'bilibili.com': 'B站',
  'www.youtube.com': 'YouTube',
  'youtube.com': 'YouTube',
  'youtu.be': 'YouTube',
  'www.douyin.com': '抖音',
  'douyin.com': '抖音',
  'www.xiaohongshu.com': '小红书',
  'xiaohongshu.com': '小红书',
}

function hueOf(runId: string): number {
  let h = 0
  for (const c of runId) h = (h * 31 + c.charCodeAt(0)) % 360
  return h
}

/** 标题关键词启发式归类（后端无 category 概念；误差可接受，tabs 交互不变） */
function categoryOf(text: string): Book['category'] {
  if (/微积分|数学|统计|线性代数|概率|几何|离散/.test(text)) return 'math'
  if (/商业|经济|管理|营销|金融|创业|电商/.test(text)) return 'econ'
  if (/英语|语言|日语|雅思|托福|写作|口语/.test(text)) return 'lang'
  return 'ai' // 强化学习/机器学习/Python/大模型/计算机默认 ai 桶
}

function toBook(r: RunListResp['items'][number], pref: { fav: boolean; progress: number }, mode: string): Book {
  let host: string
  try {
    host = new URL(r.url).hostname
  } catch {
    host = r.url
  }
  const title = (r.title || '').trim() || host || r.run_id
  const total = r.total ?? 0
  return {
    id: r.run_id,
    run_id: r.run_id,
    title,
    subtitle: r.url, // 诚实显示源地址
    hue: hueOf(r.run_id),
    category: categoryOf(title),
    progress: pref.progress,
    fav: pref.fav,
    source_badge: HOST_BADGE[host] ?? host,
    source_kind: '视频转录教材',
    pages: total, // run 无「页」概念：用讲数（条目数）填封面左下，单位换「讲」
    pages_unit: '讲',
    footer_badge: r.status === 'done' ? '已完成' : '部分完成',
    features: [],
    toc_count: total,
    read_meta: r.done_entries > 0 ? `已转录 ${r.done_entries}/${total} 讲` : undefined,
    status: r.status,
    created: r.created ?? undefined,
    bundle_ready: mode === 'deliver' && !!r.artifacts?.present,
  }
}

export async function getBooks(): Promise<Book[]> {
  const [runs, prefs] = await Promise.all([request<RunListResp>('/runs?limit=500'), request<PrefsResp>('/prefs')])
  prefsCache = new Map(prefs.items.map((p) => [p.run_id, { fav: p.fav, progress: p.progress }]))
  return runs.items
    .filter((r) => (r.status === 'done' || r.status === 'partial') && r.done_entries > 0)
    .map((r) => toBook(r, prefsCache.get(r.run_id) ?? { fav: false, progress: 0 }, runs.mode))
}

export async function toggleFav(id: string): Promise<void> {
  const cur = prefsCache.get(id) ?? { fav: false, progress: 0 }
  const next = { ...cur, fav: !cur.fav }
  prefsCache.set(id, next)
  try {
    await request(`/prefs/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ fav: next.fav }),
    })
  } catch (e) {
    prefsCache.set(id, cur) // 回翻缓存（store 层还有乐观翻转回滚）
    throw e
  }
}

/** 全书笔记（含正文 md）：阅读器数据源。错误（deliver 已焚 410 等）→ []，由视图走诚实空态。 */
export async function getNotes(bookId: string): Promise<{ title: string; md: string }[]> {
  try {
    const resp = await request<RunNotesResp>(`/runs/${bookId}/note`)
    return resp.notes.filter((n) => n.md.trim()).map((n) => ({ title: n.title, md: n.md }))
  } catch (error) {
    if (error instanceof ApiRequestError && error.code === 410) return []
    throw error
  }
}

export async function getMedia(bookId: string, name: string): Promise<Blob> {
  const path = `/runs/${encodeURIComponent(bookId)}/media/${encodeURIComponent(name)}`
  return /\.mp4$/i.test(name) ? requestMediaBlob(path) : requestBlob(path)
}

/** 阅读进度入库（切讲时调用）；成功后同步模块级缓存保持 toggleFav 合并正确，失败 throw 由调用方 toast。 */
export async function saveProgress(bookId: string, progress: number): Promise<void> {
  const p = Math.min(100, Math.max(0, Math.round(progress)))
  const cur = prefsCache.get(bookId) ?? { fav: false, progress: 0 }
  await request(`/prefs/${bookId}`, {
    method: 'PUT',
    body: JSON.stringify({ progress: p }),
  })
  prefsCache.set(bookId, { ...cur, progress: p })
}

export async function getToc(bookId: string): Promise<TocChapter[]> {
  try {
    const resp = await request<RunNotesResp>(`/runs/${bookId}/note`)
    if (resp.notes.length) {
      return resp.notes.map((n, i) => ({
        no: i + 1,
        title: n.title,
        meta: n.path.split('/').pop() ?? '',
        state: 'done' as const,
      }))
    }
  } catch {
    /* deliver 模式产物已焚（410）等：回落 entries 档 */
  }
  const detail = await request<RunDetailResp>(`/runs/${bookId}`)
  const entries = (detail.entries ?? []).filter((e) => !e.skipped)
  return entries.map((e, i) => ({
    no: i + 1,
    title: e.note?.replace(/\.md$/, '') ?? e.url,
    meta: e.url,
    state: 'done' as const,
  }))
}
