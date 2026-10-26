// 与后端契约对齐的共享类型（参照 backend/video2pdf/models.py 与 docs/architecture/03-api-contract.md）

/** 后端 run 状态词表（多出的 paused 是前端本地态） */
export type RunStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'paused'
  | 'cancelling'
  | 'done'
  | 'partial'
  | 'error'

/** 后端错误信封 */
export interface ApiError {
  error: {
    code: string | number
    message: string
    details?: unknown
  }
}

/** GET /runs 列表项，字段名与 RunListOut 对齐。 */
export interface RunListItem {
  run_id: string
  url: string
  title: string
  status: RunStatus
  created: string
  /** 0-100；running 时有意义 */
  progress: number
  /** queued 时的排队位次（1-based） */
  queue_pos?: number
  /** 预计剩余秒数 */
  eta_sec?: number
  duration_label?: string
  source_label?: string
  quality_label?: string
  /** deliver 模式产物就绪标记 */
  bundle_ready?: boolean
  readable?: boolean
  usage?: RunUsage | null
  cost_usd?: number | null
  diagnostic?: { stage: string; message: string } | null
  error?: { code: string; message: string }
}

/** 教材库条目（前端领域模型，由 done 的 run 派生） */
export interface Book {
  id: string
  run_id: string
  title: string
  subtitle: string
  /** 封面渐变色相 0-360 */
  hue: number
  category: BookCategory
  /** 阅读进度 0-100 */
  progress: number
  fav: boolean
  /** 来源徽章，如 MIT 开放公开课 / B站 30集连播 */
  source_badge: string
  source_kind: string
  pages: number
  /** 封面数量单位；run 无页数概念时使用讲数“讲”。 */
  pages_unit?: string
  footer_badge: string
  features: { icon: string; label: string; color: string }[]
  toc_count: number
  /** 滚动横幅补充信息 */
  read_meta?: string
  /** 源 run 状态（live 档：横幅「最近精讲」选书用） */
  status?: RunStatus
  /** 源 run 创建时间（created 倒序选最新） */
  created?: string
}

/** 教材目录章节（TocSheet 用；live 档由 /runs/{id}/note 派生） */
export interface TocChapter {
  no: number
  title: string
  meta: string
  state: 'done' | 'current' | 'locked' | 'quiz'
}

export type BookCategory = 'ai' | 'math' | 'econ' | 'lang'

export interface CategoryChip {
  key: BookCategory | 'all' | 'fav'
  label: string
  icon?: string
}

/** 当前登录用户 */
export interface User {
  uid: string
  name: string
  display_name: string
  avatar_hue: number
  plan: 'free' | 'pro' | 'svip'
  id_label: string
}

/** 配额与统计 */
export interface Quota {
  ai_hours_used: number
  ai_hours_total: number
  storage_gb_used: number
  storage_gb_total: number
  stats: {
    books: number
    knowledge_points: number
    accuracy: number
  }
}

/** 服务端结算账本；费用为空表示尚未结算，零值不代表已完成定价。 */
export interface RunUsage {
  llm_in_tok: number
  llm_out_tok: number
  llm_cached_in_tok?: number
  gpu_s: number
}

export interface CostReport {
  month: string
  spend_usd: number
  budget_usd: number
  usage: RunUsage & { runs_n: number }
  prices: Record<string, number>
  gpu_policy: 'metered' | 'free'
  pricing_configured: boolean
  runs: Array<{
    run_id: string
    title: string
    status: RunStatus
    created: string
    deleted: boolean
    usage: RunUsage | null
    cost_usd: number | null
    cost_pricing: Record<string, unknown> | null
  }>
}
