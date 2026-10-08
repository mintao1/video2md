<script setup lang="ts">
defineProps<{ fav: boolean }>()
const emit = defineEmits<{ (e: 'toggle'): void }>()
</script>

<template>
  <button
    :aria-label="fav ? '已收藏' : '收藏本书'"
    class="fav-toggle transition-colors"
    :class="fav ? 'text-tertiary' : 'text-outline hover:text-tertiary'"
    @click.stop="emit('toggle')"
  >
    <span class="material-symbols-outlined text-[20px]" :class="{ filled: fav }">
      {{ fav ? 'bookmark' : 'bookmark_border' }}
    </span>
  </button>
</template>
