<script setup lang="ts">
import type { Book } from '../../types'
import BookCover from '../art/BookCover.vue'
import FavBookmarkBtn from './FavBookmarkBtn.vue'

defineProps<{ book: Book }>()

const emit = defineEmits<{
  (e: 'toggleFav', id: string): void
  (e: 'read', book: Book): void
}>()
</script>

<template>
  <article
    class="book-card flex flex-col gap-space-xs rounded-xl bg-surface-container-lowest p-space-sm shadow-sm active:scale-[0.98] transition-transform cursor-pointer"
    @click="emit('read', book)"
  >
    <div class="relative w-full aspect-[3/4]">
      <BookCover
        :hue="book.hue"
        :title="book.title"
        :badge="book.source_badge"
        :footer-left="`${book.pages} ${book.pages_unit ?? '页'}`"
      />
      <div class="absolute top-1 right-1">
        <FavBookmarkBtn :fav="book.fav" @toggle="emit('toggleFav', book.id)" />
      </div>
    </div>
    <h3 class="font-body-sm text-body-sm text-on-surface font-semibold line-clamp-2 leading-snug">
      {{ book.title }}
    </h3>
    <div class="w-full h-1 bg-surface-container rounded-full overflow-hidden">
      <div
        class="h-full rounded-full bg-primary transition-all"
        :style="{ width: `${book.progress}%` }"
      />
    </div>
  </article>
</template>
