import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '../api'
import type { Book } from '../types'

export type ViewMode = 'list' | 'grid'

const VIEW_KEY = 'v2md_library_view'

export const useLibraryStore = defineStore('library', () => {
  const books = ref<Book[]>([])
  const loading = ref(false)
  const query = ref('')
  const category = ref<'all' | 'fav' | Book['category']>('all')
  const viewMode = ref<ViewMode>((localStorage.getItem(VIEW_KEY) as ViewMode) || 'list')

  const filtered = computed(() => {
    const q = query.value.trim().toLowerCase()
    return books.value.filter((b) => {
      const matchCat =
        category.value === 'all' ||
        (category.value === 'fav' && b.fav) ||
        category.value === b.category
      const matchQ =
        !q || b.title.toLowerCase().includes(q) || b.subtitle.toLowerCase().includes(q)
      return matchCat && matchQ
    })
  })

  /** 「继续阅读」横幅取进度 (0,100) 中最近的一本；无在读书籍则不显示横幅 */
  const featured = computed(() => {
    const inProgress = books.value.filter((b) => b.progress > 0 && b.progress < 100)
    return inProgress.sort((a, b) => b.progress - a.progress)[0] ?? null
  })

  async function load() {
    loading.value = true
    try {
      books.value = await api.getBooks()
    } finally {
      loading.value = false
    }
  }

  async function toggleFav(id: string) {
    const b = books.value.find((x) => x.id === id)
    if (!b) return
    b.fav = !b.fav
    try {
      await api.toggleFav(id)
    } catch {
      b.fav = !b.fav // 回滚
    }
  }

  function setViewMode(mode: ViewMode) {
    viewMode.value = mode
    localStorage.setItem(VIEW_KEY, mode)
  }

  function resetFilters() {
    query.value = ''
    category.value = 'all'
  }

  return {
    books,
    loading,
    query,
    category,
    viewMode,
    filtered,
    featured,
    load,
    toggleFav,
    setViewMode,
    resetFilters,
  }
})
