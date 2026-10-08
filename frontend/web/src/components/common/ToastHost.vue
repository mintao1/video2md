<script setup lang="ts">
import { useToastStore } from '../../stores/toast'

const toast = useToastStore()
</script>

<template>
  <div
    v-if="toast.items.length"
    class="fixed top-20 left-1/2 -translate-x-1/2 z-[100] flex flex-col items-center gap-2 pointer-events-none"
  >
    <TransitionGroup name="toast">
      <div
        v-for="t in toast.items"
        :key="t.id"
        class="flex items-center gap-2 px-4 py-2 rounded-full bg-inverse-surface text-inverse-on-surface text-body-sm shadow-xl animate-fade-in"
      >
        <span class="material-symbols-outlined text-[18px] text-primary-fixed-dim">info</span>
        <span>{{ t.message }}</span>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-enter-active,
.toast-leave-active {
  transition: all 0.25s ease;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
