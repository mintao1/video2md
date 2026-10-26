<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useLibraryStore } from '../stores/library'
import { useToastStore } from '../stores/toast'
import { getMedia, getNotes, getToc } from '../api'
import type { Book, TocChapter } from '../types'
import { printNotesAsPdf } from '../pdf'
import AppHeader from '../components/common/AppHeader.vue'
import BottomNav from '../components/common/BottomNav.vue'
import FilterChips from '../components/common/FilterChips.vue'
import EmptyState from '../components/common/EmptyState.vue'
import BookCard from '../components/book/BookCard.vue'
import BookGridItem from '../components/book/BookGridItem.vue'
import ContinueReadingBanner from '../components/book/ContinueReadingBanner.vue'
import TocSheet from '../components/book/TocSheet.vue'
import ExportFormatModal from '../components/book/ExportFormatModal.vue'

const lib = useLibraryStore()
const toast = useToastStore()
const router = useRouter()

const tocOpen = ref(false)
const exportOpen = ref(false)
const activeBook = ref<Book | null>(null)
const tocChapters = ref<TocChapter[]>([])
const syncedLabel = computed(() => {
  const at = lib.loadedAt
  if (!at) return '尚未同步'
  return `上次同步于 ${at.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`
})

const chips = computed(() => [
  { key: 'all', label: '全部教材', icon: 'auto_stories', count: lib.books.length },
  { key: 'ai', label: '计算机 / AI' },
  { key: 'math', label: '数学基础' },
  { key: 'econ', label: '商业管理' },
  { key: 'lang', label: '语言学习' },
  { key: 'fav', label: '收藏夹', icon: 'bookmark' },
])

onMounted(async () => {
  try {
    await lib.load()
  } catch {
    toast.show('教材库暂时无法加载，请检查网络后重试')
  }
})

async function openToc(book: Book) {
  activeBook.value = book
  tocChapters.value = await getToc(book.id)
  tocOpen.value = true
}

function openExport(book: Book) {
  activeBook.value = book
  exportOpen.value = true
}

function onRead(book: Book) {
  // 进阅读器：起始讲由阅读器按 prefs 进度反推（0 → 第 1 讲）
  void router.push({ name: 'read', params: { id: book.id } })
}

function onShare(book: Book) {
  void book
  toast.show('已复制教材分享链接到剪贴板！')
}

async function onExport(format: string) {
  const book = activeBook.value
  if (!book) return
  if (format === 'PDF') {
    // Open synchronously from the click handler so browsers do not classify the
    // print window as an unsolicited popup while notes/media are loading.
    const popup = window.open('', '_blank', 'popup,width=960,height=1200')
    if (!popup) {
      toast.show('浏览器阻止了打印窗口，请允许本站弹窗后重试')
      return
    }
    try {
      toast.show('正在准备 PDF…')
      const notes = await getNotes(book.id)
      if (!notes.length) throw new Error('暂无可导出的笔记')
      await printNotesAsPdf(book.title, notes, (name) => getMedia(book.id, name), popup)
      toast.show('PDF 打印窗口已打开，请选择“保存为 PDF”')
    } catch (error) {
      popup.close()
      toast.show(error instanceof Error ? error.message : 'PDF 导出失败，请稍后重试')
    }
    return
  }
  if (format !== 'Markdown') {
    toast.show(`${format} 导出暂未开放，请先选择 Markdown`)
    return
  }
  try {
    const notes = await getNotes(book.id)
    if (!notes.length) throw new Error('暂无可导出的笔记')
    const content = notes.map((note) => note.md.trim()).join('\n\n') + '\n'
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
    const href = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = href
    anchor.download = `${book.title.replace(/[\\/:*?"<>|]/g, '_')}.md`
    anchor.click()
    URL.revokeObjectURL(href)
    toast.show('Markdown 已开始下载')
  } catch (error) {
    toast.show(error instanceof Error ? error.message : 'Markdown 导出失败，请稍后重试')
  }
}

function onOpenChapter(ch: TocChapter) {
  if (activeBook.value) {
    void router.push({
      name: 'read',
      params: { id: activeBook.value.id },
      query: { ch: ch.no },
    })
  }
}

function onClearSearch() {
  lib.query = ''
}
</script>

<template>
  <div class="min-h-screen bg-surface">
    <AppHeader subtitle="Textbook Library" @notifications="toast.show('暂无新通知')" />

    <main class="flex flex-col relative w-full pt-16 pb-nav bg-surface min-h-screen">
      <!-- 搜索 -->
      <section class="px-margin pt-space-md flex flex-col gap-space-sm">
        <div class="relative flex items-center w-full">
          <span class="material-symbols-outlined absolute left-space-md text-outline pointer-events-none text-[20px]">
            search
          </span>
          <input
            v-model="lib.query"
            class="w-full h-11 pl-11 pr-10 rounded-full bg-surface-container-low text-on-surface placeholder:text-outline font-body-md text-body-md shadow-sm focus:outline-none focus:bg-surface-container transition-all"
            placeholder="搜索已生成的教材、科目或知识点..."
            type="text"
          />
          <button
            v-if="lib.query"
            aria-label="清空搜索"
            class="absolute right-space-sm w-7 h-7 rounded-full bg-surface-container text-on-surface-variant flex items-center justify-center transition-transform active:scale-90"
            @click="onClearSearch"
          >
            <span class="material-symbols-outlined text-[16px]">close</span>
          </button>
        </div>

        <!-- 分类筛选 -->
        <FilterChips v-model="lib.category" :options="chips" />
      </section>

      <!-- 继续阅读横幅 -->
      <ContinueReadingBanner
        v-if="lib.featured && lib.category === 'all' && !lib.query"
        :book="lib.featured"
        @read="onRead"
        @toc="openToc"
      />

      <!-- 书架标题 + 视图切换 -->
      <section class="px-margin pt-space-lg flex items-center justify-between">
        <div class="flex items-baseline gap-space-xs">
          <h2 class="font-headline-lg-mobile text-headline-lg-mobile text-on-surface">智能教材架</h2>
          <span class="font-label-sm text-label-sm text-outline">共 {{ lib.filtered.length }} 本</span>
        </div>
        <div class="flex items-center bg-surface-container-low rounded-lg p-0.5">
          <button
            aria-label="网格视图"
            class="w-8 h-8 rounded flex items-center justify-center transition-all"
            :class="
              lib.viewMode === 'grid'
                ? 'bg-surface-container-lowest shadow-sm text-primary font-semibold'
                : 'text-on-surface-variant hover:text-on-surface'
            "
            @click="lib.setViewMode('grid')"
          >
            <span class="material-symbols-outlined text-[18px]">grid_view</span>
          </button>
          <button
            aria-label="列表视图"
            class="w-8 h-8 rounded flex items-center justify-center transition-all"
            :class="
              lib.viewMode === 'list'
                ? 'bg-surface-container-lowest shadow-sm text-primary font-semibold'
                : 'text-on-surface-variant hover:text-on-surface'
            "
            @click="lib.setViewMode('list')"
          >
            <span class="material-symbols-outlined text-[18px]">view_agenda</span>
          </button>
        </div>
      </section>

      <!-- 书列表 -->
      <section
        class="px-margin pt-space-sm"
        :class="lib.viewMode === 'grid' ? 'grid grid-cols-3 gap-space-sm' : 'flex flex-col gap-space-md'"
      >
        <template v-if="lib.viewMode === 'list'">
          <BookCard
            v-for="b in lib.filtered"
            :key="b.id"
            :book="b"
            @toggle-fav="(id) => lib.toggleFav(id)"
            @read="onRead"
            @toc="openToc"
            @export="openExport"
            @share="onShare"
          />
        </template>
        <template v-else>
          <BookGridItem
            v-for="b in lib.filtered"
            :key="b.id"
            :book="b"
            @toggle-fav="(id) => lib.toggleFav(id)"
            @read="onRead"
          />
        </template>

        <EmptyState
          v-if="lib.filtered.length === 0 && !lib.loading"
          title="未找到相关教材"
          description="试试搜索其他关键词，或复制新视频链接生成专属教材吧。"
          action-label="清除筛选条件"
          @action="lib.resetFilters()"
        />
      </section>

      <!-- 同步状态条 -->
      <section class="px-margin pt-space-lg pb-space-md">
        <div class="flex items-center justify-between p-space-sm px-space-md rounded-xl bg-surface-container-low">
          <div class="flex items-center gap-2">
            <span class="material-symbols-outlined text-primary text-[20px]">cloud_sync</span>
            <div class="flex flex-col">
              <span class="font-label-md text-label-md text-on-surface">全端书籍与笔记自动同步已开启</span>
              <span class="font-label-sm text-label-sm text-outline">{{ syncedLabel }}</span>
            </div>
          </div>
          <button
            class="text-primary font-label-md text-label-md flex items-center gap-0.5 active:scale-95 transition-transform"
            @click="toast.show('已完成云端同步：所有教材与高亮标记已是最新！')"
          >
            <span>立即同步</span>
            <span class="material-symbols-outlined text-[16px]">sync</span>
          </button>
        </div>
      </section>
    </main>

    <TocSheet
      v-model="tocOpen"
      :book-title="activeBook?.title ?? ''"
      :chapters="tocChapters"
      @open-chapter="onOpenChapter"
    />
    <ExportFormatModal
      v-model="exportOpen"
      :book-title="activeBook?.title ?? ''"
      @export="onExport"
    />

    <BottomNav />
  </div>
</template>
