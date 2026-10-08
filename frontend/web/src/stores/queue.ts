import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '../api'
import type { RunListItem } from '../types'

export type QueueFilter = 'all' | 'active' | 'queued' | 'done'

export const useQueueStore = defineStore('queue', () => {
  const runs = ref<RunListItem[]>([])
  const loading = ref(false)
  const filter = ref<QueueFilter>('all')
  const batchMode = ref(false)
  const selected = ref<Set<string>>(new Set())

  const filtered = computed(() => {
    switch (filter.value) {
      case 'active':
        return runs.value.filter((r) => r.status === 'running' || r.status === 'paused')
      case 'queued':
        return runs.value.filter((r) => r.status === 'queued')
      case 'done':
        return runs.value.filter((r) => ['done', 'partial', 'error'].includes(r.status))
      default:
        return runs.value
    }
  })

  /** BottomNav 角标 = 进行中（running/paused + queued） */
  const activeCount = computed(
    () => runs.value.filter((r) => r.status === 'running' || r.status === 'queued' || r.status === 'paused').length,
  )

  const counts = computed(() => ({
    all: runs.value.length,
    active: runs.value.filter((r) => r.status === 'running' || r.status === 'paused').length,
    queued: runs.value.filter((r) => r.status === 'queued').length,
    done: runs.value.filter((r) => ['done', 'partial', 'error'].includes(r.status)).length,
  }))

  async function load() {
    loading.value = true
    try {
      runs.value = await api.listRuns()
    } finally {
      loading.value = false
    }
  }

  async function createRun(url: string, title?: string) {
    const run = await api.createRun({ url, title })
    runs.value.unshift(run)
  }

  async function pause(id: string) {
    await api.cancelRun(id)
    const r = runs.value.find((x) => x.run_id === id)
    if (r) {
      r.status = 'paused'
      r.eta_sec = undefined
    }
  }

  async function resume(id: string) {
    await api.resumeRun(id)
    const r = runs.value.find((x) => x.run_id === id)
    if (r) {
      r.status = 'running'
      r.eta_sec = 300
    }
  }

  async function promote(id: string) {
    await api.promoteRun(id)
    await load()
  }

  async function removeSelected() {
    const ids = [...selected.value]
    await api.deleteRuns(ids)
    runs.value = runs.value.filter((r) => !ids.includes(r.run_id))
    selected.value = new Set()
    batchMode.value = false
  }

  function toggleSelect(id: string) {
    const next = new Set(selected.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    selected.value = next
  }

  function selectAll() {
    selected.value = new Set(filtered.value.map((r) => r.run_id))
  }

  function clearSelection() {
    selected.value = new Set()
  }

  function exitBatchMode() {
    batchMode.value = false
    selected.value = new Set()
  }

  // ---- mock 进度推进（真实 API 下由 SSE /runs/{id}/events 驱动，直接删掉即可）----
  let timer: ReturnType<typeof setInterval> | null = null

  function startProgressTick() {
    if (timer) return
    timer = setInterval(() => {
      let changed = false
      for (const r of runs.value) {
        if (r.status !== 'running') continue
        r.progress = Math.min(100, r.progress + Math.random() * 2)
        if (r.eta_sec && r.eta_sec > 0) r.eta_sec = Math.max(0, r.eta_sec - 3)
        api._setProgress({ run_id: r.run_id, progress: r.progress, eta_sec: r.eta_sec })
        changed = true
        if (r.progress >= 100) {
          r.status = 'done'
          r.bundle_ready = true
          r.progress = 100
          api._setProgress({ run_id: r.run_id, status: 'done', progress: 100, bundle_ready: true })
        }
      }
      // running 空位时，把队首 queued 任务顶上（模拟并发闸门放行）
      if (!runs.value.some((r) => r.status === 'running')) {
        const next = runs.value.find((r) => r.status === 'queued')
        if (next) {
          next.status = 'running'
          next.eta_sec = 300
          api._setProgress({ run_id: next.run_id, status: 'running', eta_sec: 300 })
        }
      }
      // 重排排队位次
      let pos = 1
      for (const r of runs.value) {
        if (r.status === 'queued') {
          r.queue_pos = pos++
        } else if (r.queue_pos !== undefined) {
          r.queue_pos = undefined
        }
      }
      void changed
    }, 3000)
  }

  function stopProgressTick() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  return {
    runs,
    loading,
    filter,
    batchMode,
    selected,
    filtered,
    activeCount,
    counts,
    load,
    createRun,
    pause,
    resume,
    promote,
    removeSelected,
    toggleSelect,
    selectAll,
    clearSelection,
    exitBatchMode,
    startProgressTick,
    stopProgressTick,
  }
})
