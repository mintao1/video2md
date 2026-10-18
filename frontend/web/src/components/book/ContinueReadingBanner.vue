<script setup lang="ts">
import type { Book } from '../../types'
import BookCover from '../art/BookCover.vue'

defineProps<{ book: Book }>()

const emit = defineEmits<{
  (e: 'read', book: Book): void
  (e: 'toc', book: Book): void
}>()
</script>

<template>
  <section class="px-margin pt-space-md">
    <div
      class="relative overflow-hidden rounded-xl bg-gradient-to-br from-primary via-secondary to-primary-container text-on-primary p-space-md shadow-lg shadow-primary/20"
    >
      <div class="absolute -right-8 -bottom-10 opacity-15 pointer-events-none">
        <span class="material-symbols-outlined text-[180px]">menu_book</span>
      </div>
      <div class="flex items-center justify-between pb-space-xs">
        <div
          class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-on-primary/15 backdrop-blur-sm text-inverse-on-surface font-label-sm text-label-sm"
        >
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
          <span>
            {{ book.progress > 0 ? `最近在读 · 进度 ${book.progress}%` : '最近精讲' }}
          </span>
        </div>
        <span v-if="book.read_meta" class="font-label-sm text-label-sm text-primary-fixed-dim">
          {{ book.read_meta }}
        </span>
      </div>
      <div class="flex gap-space-md mt-space-sm items-center">
        <div class="relative flex-shrink-0 w-20 h-28">
          <BookCover :hue="book.hue" :title="book.title" />
        </div>
        <div class="flex flex-col min-w-0 flex-1 justify-center">
          <div class="flex items-center gap-1 text-primary-fixed-dim font-label-sm text-label-sm">
            <span class="material-symbols-outlined text-[15px]">auto_awesome</span>
            <span class="truncate">{{ book.title }}</span>
          </div>
          <p class="font-headline-md text-headline-md tracking-tight truncate mt-0.5 text-on-primary">
            {{ book.subtitle }}
          </p>
          <div class="mt-space-sm flex items-center gap-2">
            <button
              class="px-4 py-2 rounded-lg bg-surface-container-lowest text-primary font-title-sm text-title-sm flex items-center gap-1.5 shadow-md active:scale-95 transition-all"
              @click="emit('read', book)"
            >
              <span class="material-symbols-outlined text-[18px]">play_circle</span>
              <span>继续阅读</span>
            </button>
            <button
              aria-label="查看全书目录"
              class="w-9 h-9 rounded-lg bg-on-primary/15 text-on-primary flex items-center justify-center hover:bg-on-primary/25 transition-colors"
              @click="emit('toc', book)"
            >
              <span class="material-symbols-outlined text-[18px]">account_tree</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
