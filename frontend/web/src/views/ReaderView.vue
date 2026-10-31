<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { getBooks, getNotes, getMedia, saveProgress } from '../api'
import { chapterIndex, extractCallouts, mediaMarkdown, mediaNames, startMediaLoad } from '../reader'
import { useToastStore } from '../stores/toast'
import type { Book, TocChapter } from '../types'
import EmptyState from '../components/common/EmptyState.vue'
import TocSheet from '../components/book/TocSheet.vue'

// 笔记阅读器：GET /runs/{id}/note 的 markdown 渲染 + 章节（讲）导航；
// 切讲时把进度写回 /prefs（PUT）——「继续阅读」/横幅「最近在读」的数据源。
// 起始讲：?ch=N（目录跳转）优先，否则由 prefs 进度反推 round(p/100*n)。
const route = useRoute()
const router = useRouter()
const toast = useToastStore()

const runId = computed(() => String(route.params.id))
const book = ref<Book | null>(null)
const notes = ref<{ title: string; md: string }[]>([])
const idx = ref(0) // 0-based 当前讲
const loading = ref(true)
const loadError = ref('')
const mediaUrls = ref<Record<string, string>>({})
const pendingMedia = ref<string[]>([])
let loadVersion = 0
let stopMedia = () => {}

function releaseMedia() {
  stopMedia()
  mediaUrls.value = {}
  pendingMedia.value = []
}

watch(() => [runId.value, notes.value[idx.value]?.md] as const, ([id, md]) => {
  releaseMedia()
  pendingMedia.value = mediaNames(md ?? '')
  stopMedia = startMediaLoad(pendingMedia.value, (name) => getMedia(id, name), (name, url) => {
    if (url) mediaUrls.value = { ...mediaUrls.value, [name]: url }
    pendingMedia.value = pendingMedia.value.filter((item) => item !== name)
  })
})

onBeforeUnmount(() => {
  loadVersion++
  releaseMedia()
})
const tocOpen = ref(false)
const contentEl = ref<HTMLElement | null>(null)

const total = computed(() => notes.value.length)
const progress = computed(() =>
  total.value ? Math.round(((idx.value + 1) / total.value) * 100) : 0,
)
const html = computed(() =>
  notes.value[idx.value] ? renderMd(notes.value[idx.value].md) : '',
)
const chapters = computed<TocChapter[]>(() =>
  notes.value.map((n, i) => ({
    no: i + 1,
    title: n.title,
    meta: `第 ${i + 1} / ${total.value} 讲`,
    state: i < idx.value ? ('done' as const) : i === idx.value ? ('current' as const) : ('locked' as const),
  })),
)

function renderMd(md: string): string {
  // md 源自外部视频转录经 LLM 生成 → v-html 必须过 DOMPurify（防注入，token 在 localStorage）
  const extracted = extractCallouts(md)
  let source = mediaMarkdown(extracted.markdown, mediaUrls.value, pendingMedia.value)
  const calloutHtml = extracted.callouts.map((callout) => {
    const body = marked.parse(mediaMarkdown(callout.body, mediaUrls.value, pendingMedia.value), { async: false }) as string
    const open = callout.fold ? '' : ' open'
    return `<aside class="md-callout md-callout-${callout.type}" data-callout-type="${callout.type}"><details${open}><summary>${callout.title}</summary><div class="md-callout-body">${body}</div></details></aside>`
  })
  extracted.callouts.forEach((_callout, index) => {
    source = source.replace(`<!--V2MD_CALLOUT_${index}-->`, calloutHtml[index] ?? '')
  })
  const fragment = DOMPurify.sanitize(
    marked.parse(source, { async: false }) as string,
    { RETURN_DOM_FRAGMENT: true },
  )
  // DOMPurify intentionally strips blob: URIs. Restore only object URLs created
  // by our authenticated media loader, after sanitizing all generated markup.
  for (const element of fragment.querySelectorAll('img[data-v2md-media], video[data-v2md-media]')) {
    const name = element.getAttribute('data-v2md-media') ?? ''
    const url = mediaUrls.value[name]
    if (url?.startsWith('blob:')) element.setAttribute('src', url)
    element.removeAttribute('data-v2md-media')
  }
  const container = document.createElement('div')
  container.append(fragment)
  return container.innerHTML
}

async function goTo(no: number): Promise<void> {
  const n = total.value
  if (n === 0) return
  const next = chapterIndex(no, n) + 1
  if (next - 1 === idx.value) return
  idx.value = next - 1
  contentEl.value?.scrollTo({ top: 0 })
  try {
    // 章节粒度进度：读到第 k 讲 = round(k/n*100)；末讲 = 100（读完，横幅「最近在读」退场）
    await saveProgress(runId.value, Math.round((next / n) * 100))
  } catch {
    toast.show('阅读进度保存失败，稍后重试')
  }
}

watch(() => [route.params.id, route.query.ch], async () => {
  const version = ++loadVersion
  loading.value = true
  loadError.value = ''
  try {
    const [books, loaded] = await Promise.all([getBooks(), getNotes(runId.value)])
    if (version !== loadVersion) return
    book.value = books.find((b) => b.id === runId.value) ?? null
    notes.value = loaded
    idx.value = chapterIndex(route.query.ch, loaded.length, book.value?.progress)
    if (loaded.length) {
      try { await saveProgress(runId.value, progress.value) }
      catch { toast.show('阅读进度保存失败，稍后重试') }
    }
  } catch (error) {
    if (version !== loadVersion) return
    notes.value = []
    loadError.value = error instanceof Error ? error.message : '加载失败，请稍后重试'
    toast.show(loadError.value)
  } finally {
    if (version === loadVersion) loading.value = false
  }
}, { immediate: true })

</script>

<template>
  <div class="min-h-screen bg-surface flex flex-col max-w-[430px] mx-auto">
    <!-- 头部 -->
    <header
      class="sticky top-0 z-10 bg-surface/95 backdrop-blur border-b border-outline-variant/40 px-4 py-3 flex items-center gap-2"
    >
      <button
        class="w-9 h-9 rounded-full flex items-center justify-center text-on-surface hover:bg-surface-container transition-colors shrink-0"
        aria-label="返回教材库"
        @click="router.push('/library')"
      >
        <span class="material-symbols-outlined text-[22px]">arrow_back</span>
      </button>
      <div class="min-w-0 flex-1">
        <h1 class="font-title-sm text-title-sm text-on-surface truncate">
          {{ book?.title ?? '精讲笔记' }}
        </h1>
        <p v-if="total" class="font-label-sm text-label-sm text-outline truncate">
          第 {{ idx + 1 }} / {{ total }} 讲 · 进度 {{ progress }}%
        </p>
      </div>
      <button
        v-if="total"
        class="w-9 h-9 rounded-full flex items-center justify-center text-on-surface hover:bg-surface-container transition-colors shrink-0"
        aria-label="打开目录"
        @click="tocOpen = true"
      >
        <span class="material-symbols-outlined text-[22px]">format_list_bulleted</span>
      </button>
    </header>

    <!-- 正文 -->
    <main ref="contentEl" class="flex-1 overflow-y-auto">
      <div v-if="loading" class="flex items-center justify-center py-24">
        <span class="material-symbols-outlined animate-spin text-4xl text-primary"
          >progress_activity</span
        >
      </div>

      <div
        v-else-if="!notes.length"
        class="flex items-center justify-center px-6 py-24"
      >
        <EmptyState
          icon="menu_book"
          title="暂无精讲笔记"
          :description="loadError || '该教材的笔记产物尚未生成或已被焚毁。可回到教材库查看转录状态，或稍后再试。'"
          action-label="返回教材库"
          @action="router.push('/library')"
        />
      </div>

      <article
        v-else
        class="prose-v2md text-on-surface px-5 py-5 pb-24"
        v-html="html"
      />
    </main>

    <!-- 底部章节导航 -->
    <footer
      v-if="total"
      class="sticky bottom-0 bg-surface border-t border-outline-variant/40 px-4 py-3 flex items-center gap-2"
    >
      <button
        class="flex-1 py-2.5 rounded-lg font-title-sm text-label-md flex items-center justify-center gap-1 transition-colors"
        :class="
          idx === 0
            ? 'bg-surface-container-low text-outline'
            : 'bg-surface-container-high text-on-surface active:scale-95'
        "
        :disabled="idx === 0"
        @click="goTo(idx)"
      >
        <span class="material-symbols-outlined text-[18px]">chevron_left</span>
        <span>上一讲</span>
      </button>
      <button
        class="px-4 py-2.5 rounded-lg bg-surface-container text-on-surface-variant font-title-sm text-label-md flex items-center justify-center gap-1 hover:bg-surface-container-high transition-colors"
        @click="tocOpen = true"
      >
        <span class="material-symbols-outlined text-[18px]">account_tree</span>
        <span>目录</span>
      </button>
      <button
        class="flex-1 py-2.5 rounded-lg font-title-sm text-label-md flex items-center justify-center gap-1 transition-colors"
        :class="
          idx >= total - 1
            ? 'bg-surface-container-low text-outline'
            : 'bg-primary text-on-primary active:scale-95'
        "
        :disabled="idx >= total - 1"
        @click="goTo(idx + 2)"
      >
        <span>下一讲</span>
        <span class="material-symbols-outlined text-[18px]">chevron_right</span>
      </button>
    </footer>

    <TocSheet
      v-model="tocOpen"
      :book-title="book?.title ?? '精讲笔记'"
      :chapters="chapters"
      @open-chapter="goTo($event.no)"
    />
  </div>
</template>

<style scoped>
/* 手写精讲笔记排版（tailwind 无 typography 插件，不为此加插件）；颜色继承 text-on-surface */
.prose-v2md :deep(h1) {
  font-size: 1.35rem;
  font-weight: 700;
  margin: 1.25rem 0 0.75rem;
  line-height: 1.4;
}
.prose-v2md :deep(h2) {
  font-size: 1.15rem;
  font-weight: 700;
  margin: 1.25rem 0 0.5rem;
  line-height: 1.4;
}
.prose-v2md :deep(h3) {
  font-size: 1.02rem;
  font-weight: 600;
  margin: 1rem 0 0.5rem;
}
.prose-v2md :deep(p) {
  margin: 0.65rem 0;
}
.prose-v2md :deep(ul),
.prose-v2md :deep(ol) {
  padding-left: 1.4rem;
  margin: 0.65rem 0;
}
.prose-v2md :deep(ul) {
  list-style: disc;
}
.prose-v2md :deep(ol) {
  list-style: decimal;
}
.prose-v2md :deep(li) {
  margin: 0.3rem 0;
}
.prose-v2md :deep(blockquote) {
  border-left: 3px solid #cac4d0;
  padding-left: 0.9rem;
  margin: 0.75rem 0;
  color: #49454f;
}
.prose-v2md :deep(code) {
  background: #ece6f0;
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  font-size: 0.85em;
}
.prose-v2md :deep(pre) {
  background: #1f1b24;
  color: #e6e0e9;
  padding: 0.85rem 1rem;
  border-radius: 0.75rem;
  overflow-x: auto;
  margin: 0.75rem 0;
}
.prose-v2md :deep(pre code) {
  background: none;
  padding: 0;
  color: inherit;
  font-size: 0.82em;
}
.prose-v2md :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 0.75rem 0;
  font-size: 0.9em;
  display: block;
  overflow-x: auto;
}
.prose-v2md :deep(th),
.prose-v2md :deep(td) {
  border: 1px solid #cac4d0;
  padding: 0.4rem 0.55rem;
  text-align: left;
}
.prose-v2md :deep(th) {
  background: #f3edf7;
  font-weight: 600;
}
.prose-v2md :deep(a) {
  color: inherit;
  text-decoration: underline;
  word-break: break-all;
}
.prose-v2md :deep(hr) {
  border: none;
  border-top: 1px solid #cac4d0;
  margin: 1rem 0;
}
.prose-v2md :deep(.md-callout) {
  margin: 0.9rem 0;
  border-left: 4px solid #79747e;
  border-radius: 0.5rem;
  background: #f7f2fa;
  padding: 0.15rem 0.8rem;
}
.prose-v2md :deep(.md-callout-info) { border-left-color: #3f51b5; }
.prose-v2md :deep(.md-callout-tip) { border-left-color: #008577; }
.prose-v2md :deep(.md-callout-warning) { border-left-color: #b26a00; }
.prose-v2md :deep(.md-callout-example) { border-left-color: #7b1fa2; }
.prose-v2md :deep(.md-callout summary) {
  cursor: pointer;
  font-weight: 700;
  padding: 0.45rem 0;
}
.prose-v2md :deep(.md-callout-body) { padding-bottom: 0.45rem; }
.prose-v2md :deep(img) {
  max-width: 100%;
  border-radius: 0.75rem;
}
</style>
