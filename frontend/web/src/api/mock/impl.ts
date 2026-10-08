// mock 实现：内存态 + 延时。函数签名 = 未来 live 实现的签名。
import type {
  Book,
  BookCategory,
  Quota,
  RunListItem,
  RunStatus,
  SmsCodeOut,
  User,
} from '../../types'
import { mockBooks, mockQuota, mockRuns, mockToc, mockUser, type TocChapter } from './data'

const delay = (ms = 200 + Math.random() * 200) => new Promise((r) => setTimeout(r, ms))

/** 可变内存态（模块单例，跨组件共享） */
const state = {
  books: structuredClone(mockBooks),
  runs: structuredClone(mockRuns),
  user: structuredClone(mockUser),
  quota: structuredClone(mockQuota),
  toc: structuredClone(mockToc) as TocChapter[],
}

let runSeq = 0

export const impl = {
  // ---------- auth ----------
  async sendSmsCode(_phone: string): Promise<SmsCodeOut> {
    await delay()
    return { sent: true, expires_in: 300 }
  },
  async loginWithSms(_phone: string, _code: string): Promise<{ token: string; user: User }> {
    await delay(400)
    return { token: 'mock-id-token', user: state.user }
  },
  async loginWithPassword(_phone: string, _password: string): Promise<{ token: string; user: User }> {
    await delay(400)
    return { token: 'mock-id-token', user: state.user }
  },
  async loginWithWechat(): Promise<{ token: string; user: User }> {
    await delay(600)
    return { token: 'mock-id-token', user: state.user }
  },
  async currentUser(): Promise<User> {
    await delay(120)
    return state.user
  },

  // ---------- books ----------
  async getBooks(): Promise<Book[]> {
    await delay()
    return structuredClone(state.books)
  },
  async toggleFav(id: string): Promise<void> {
    await delay(120)
    const b = state.books.find((x) => x.id === id)
    if (b) b.fav = !b.fav
  },
  async getToc(_bookId: string): Promise<TocChapter[]> {
    await delay(150)
    return structuredClone(state.toc)
  },

  // ---------- runs / queue ----------
  async listRuns(): Promise<RunListItem[]> {
    await delay()
    return structuredClone(state.runs)
  },
  async createRun(body: { url: string; title?: string }): Promise<RunListItem> {
    await delay(300)
    const hasActive = state.runs.some((r) => r.status === 'running')
    const run: RunListItem = {
      run_id: `run-new-${++runSeq}`,
      url: body.url,
      title: body.title || deriveTitle(body.url),
      status: hasActive ? 'queued' : 'running',
      created: new Date().toISOString(),
      progress: 0,
      queue_pos: hasActive ? 1 : undefined,
      eta_sec: hasActive ? undefined : 420,
      duration_label: '--:--',
      source_label: guessSource(body.url),
      quality_label: '1080P 高清',
    }
    state.runs.unshift(run)
    return structuredClone(run)
  },
  async cancelRun(id: string): Promise<void> {
    await delay(150)
    const r = state.runs.find((x) => x.run_id === id)
    if (r) {
      r.status = 'paused'
      r.eta_sec = undefined
    }
  },
  async resumeRun(id: string): Promise<void> {
    await delay(150)
    const r = state.runs.find((x) => x.run_id === id)
    if (r) {
      r.status = 'running'
      r.eta_sec = 300
    }
  },
  async promoteRun(id: string): Promise<void> {
    await delay(150)
    const idx = state.runs.findIndex((x) => x.run_id === id)
    if (idx >= 0) {
      const [r] = state.runs.splice(idx, 1)
      r.queue_pos = 1
      state.runs.unshift(r)
      state.runs.forEach((x, i) => {
        if (x.status === 'queued') x.queue_pos = i + 1
      })
    }
  },
  async deleteRuns(ids: string[]): Promise<void> {
    await delay(200)
    state.runs = state.runs.filter((r) => !ids.includes(r.run_id))
  },

  // 供 store 的模拟进度 tick 批量写回（mock 专用，无对应真实 API）
  _setProgress(patch: Partial<RunListItem> & { run_id: string }): void {
    const r = state.runs.find((x) => x.run_id === patch.run_id)
    if (r) Object.assign(r, patch)
  },
  _snapshotRuns(): RunListItem[] {
    return state.runs
  },

  // ---------- user ----------
  async getQuota(): Promise<Quota> {
    await delay(150)
    return structuredClone(state.quota)
  },
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

function guessSource(url: string): string {
  if (url.includes('bilibili')) return 'Bilibili'
  if (url.includes('youtube') || url.includes('youtu.be')) return 'YouTube'
  return '链接'
}

export type { TocChapter }
export type { BookCategory, RunStatus }
