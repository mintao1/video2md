<script setup lang="ts">
import type { Book } from '../../types'
import BookCover from '../art/BookCover.vue'
import FavBookmarkBtn from './FavBookmarkBtn.vue'
import ProgressBar from '../common/ProgressBar.vue'

defineProps<{ book: Book }>()

const emit = defineEmits<{
  (e: 'toggleFav', id: string): void
  (e: 'read', book: Book): void
  (e: 'toc', book: Book): void
  (e: 'download', book: Book): void
}>()
</script>

<template>
  <article
    class="book-card rounded-xl bg-surface-container-lowest p-space-md shadow-sm transition-all flex flex-col gap-space-sm"
  >
    <div class="flex gap-space-md items-start">
      <!-- 封面 -->
      <div class="relative flex-shrink-0 w-24 h-32">
        <BookCover
          :hue="book.hue"
          :title="book.title"
          :footer-left="`${book.pages} ${book.pages_unit ?? '页'}`"
          :footer-right="book.footer_badge"
        />
      </div>

      <!-- 信息列 -->
      <div class="flex flex-col min-w-0 flex-1">
        <div class="flex items-center justify-between">
          <span
            class="font-label-sm text-label-sm font-semibold flex items-center gap-0.5 truncate"
            :class="
              book.category === 'ai' ? 'text-secondary' : book.category === 'econ' ? 'text-tertiary' : 'text-primary'
            "
          >
            <span class="material-symbols-outlined text-[14px]">school</span>
            <span class="truncate">{{ book.source_badge }}</span>
          </span>
          <FavBookmarkBtn :fav="book.fav" @toggle="emit('toggleFav', book.id)" />
        </div>
        <h3 class="font-title-sm text-title-sm text-on-surface truncate mt-1">
          {{ book.title }}
        </h3>
        <p class="font-body-sm text-body-sm text-on-surface-variant line-clamp-2 mt-1">
          {{ book.subtitle }}
        </p>
        <div v-if="book.features.length" class="flex flex-wrap gap-1 mt-2">
          <span
            v-for="f in book.features"
            :key="f.label"
            class="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-full bg-surface-container text-on-surface-variant font-label-sm text-[10px]"
          >
            <span class="material-symbols-outlined text-[12px]" :class="f.color">{{ f.icon }}</span>
            {{ f.label }}
          </span>
        </div>
      </div>
    </div>

    <!-- 阅读进度 -->
    <div class="flex flex-col gap-1 pt-1">
      <div class="flex justify-between items-center text-label-sm font-label-sm text-outline">
        <span>{{ book.read_meta ?? (book.progress > 0 ? `阅读进度：${book.progress}%` : '尚未开始阅读') }}</span>
        <span v-if="book.progress > 0" class="text-primary font-semibold">{{ book.progress }}%</span>
      </div>
      <ProgressBar
        :value="book.progress"
        :color="book.progress >= 100 ? 'bg-emerald-500' : book.progress > 0 ? 'bg-primary' : 'bg-tertiary'"
      />
    </div>

    <!-- 操作行 -->
    <div class="flex items-center justify-between pt-space-xs">
      <div class="flex items-center gap-1.5">
        <button
          class="px-3.5 py-1.5 rounded-lg font-title-sm text-label-md flex items-center gap-1 shadow-sm active:scale-95 transition-all"
          :class="
            book.progress > 0
              ? 'bg-primary text-on-primary'
              : 'bg-surface-container-high text-on-surface hover:bg-primary hover:text-on-primary'
          "
          @click="emit('read', book)"
        >
          <span class="material-symbols-outlined text-[16px]">
            {{ book.progress > 0 ? 'menu_book' : 'play_arrow' }}
          </span>
          <span>{{ book.progress > 0 ? '继续阅读' : '开始精读' }}</span>
        </button>
        <button
          class="px-2.5 py-1.5 rounded-lg bg-surface-container text-on-surface-variant font-label-md text-label-md hover:bg-surface-container-high transition-colors flex items-center gap-1"
          @click="emit('toc', book)"
        >
          <span class="material-symbols-outlined text-[16px]">format_list_bulleted</span>
          <span>目录 ({{ book.toc_count }})</span>
        </button>
      </div>
      <div class="flex items-center gap-1">
        <button
          v-if="book.bundle_ready"
          class="w-8 h-8 rounded-full text-on-surface-variant hover:bg-surface-container flex items-center justify-center transition-colors"
          title="下载 Markdown 原始包"
          aria-label="下载 Markdown 原始包"
          @click="emit('download', book)"
        >
          <span class="material-symbols-outlined text-[18px]">download</span>
        </button>
      </div>
    </div>
  </article>
</template>
