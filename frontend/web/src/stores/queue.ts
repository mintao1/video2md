import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '../api'
import type { RunListItem } from '../types'

export type QueueFilter = 'all' | 'active' | 'queued' | 'done' | 'error'

export const useQueueStore = defineStore('queue', () => {
  const runs = ref<RunListItem[]>([])
  const loading = ref(false)
  const filter = ref<QueueFilter>('all')
  const batchMode = ref(false)
  const selected = ref<Set<string>>(new Set())

  const filtered = computed(() => {
    switch (filter.value) {
      case 'active':
        return runs.value.filter((r) => r.status === 'running' || r.status === 'paused' || r.status === 'cancelling')
      case 'queued':
        return runs.value.filter((r) => r.status === 'queued')
      case 'done':
        return runs.value.filter((r) => (r.status === 'done' || r.status === 'partial') && r.readable)
      case 'error':
        return runs.value.filter((r) => r.status === 'error' || (r.status === 'partial' && !r.readable))
      default:
        return runs.value
    }
  })

  /** BottomNav 角标 = 进行中（running/paused + queued） */
  const activeCount = computed(
    () => runs.value.filter((r) => r.status === 'running' || r.status === 'queued' || r.status === 'paused' || r.status === 'cancelling').length,
  )

  const counts = computed(() => ({
    all: runs.value.length,
    active: runs.value.filter((r) => r.status === 'running' || r.status === 'paused' || r.status === 'cancelling').length,
    queued: runs.value.filter((r) => r.status === 'queued').length,
    done: runs.value.filter((r) => (r.status === 'done' || r.status === 'partial') && r.readable).length,
    error: runs.value.filter((r) => r.status === 'error' || (r.status === 'partial' && !r.readable)).length,
  }))

  let revision = 0
  async function load() {
    const version = ++revision
    loading.value = true
    try {
      const items = await api.listRuns()
      if (version === revision) runs.value = items
    } finally {
      if (version === revision) loading.value = false
    }
  }

  async function createRun(url: string, title?: string, partItems?: string) {
    const run = await api.createRun({ url, title, partItems })
    revision++
    if (runs.value.some((existing) => existing.run_id === run.run_id)) {
      await load() // Reused runs retain their authoritative usage and artifact state.
    } else {
      runs.value.unshift(run)
    }
  }

  async function pause(id: string) {
    const result = await api.cancelRun(id)
    revision++
    const r = runs.value.find((x) => x.run_id === id)
    if (r) {
      r.status = result.status
      r.eta_sec = undefined
    }
  }

  async function resume(id: string) {
    await api.resumeRun(id)
    await load()
  }

  async function promote(id: string) {
    await api.promoteRun(id)
    await load()
  }

  async function removeSelected() {
    const ids = [...selected.value]
    try {
      await api.deleteRuns(ids)
    } finally {
      await load() // 批量请求部分成功时也以服务端快照为准。
    }
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

  // 后端是真实进度唯一来源；定时器只刷新，不自行推进任务。
  let timer: ReturnType<typeof setInterval> | null = null
  let refreshing = false

  function startProgressTick() {
    if (timer) return
    timer = setInterval(async () => {
      if (refreshing) return
      refreshing = true
      try {
        await load()
      } catch {
        // 保留最后一次成功快照；网络失败不伪造完成态。
      } finally {
        refreshing = false
      }
    }, 3000)
  }

  function stopProgressTick() {
    revision++
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
