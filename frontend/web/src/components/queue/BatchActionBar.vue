<script setup lang="ts">
import { useQueueStore } from '../../stores/queue'

const queue = useQueueStore()
</script>

<template>
  <div
    class="bg-surface-container-high rounded-xl p-space-md border border-outline-variant/30 shadow-md flex items-center justify-between transition-all"
  >
    <div class="flex items-center gap-space-sm">
      <div class="flex items-center gap-1 bg-primary text-on-primary px-2 py-0.5 rounded-full font-label-sm text-[11px] font-semibold">
        <span class="material-symbols-outlined text-[14px]">done_all</span>
        <span>已选 {{ queue.selected.size }} 项</span>
      </div>
      <button
        class="font-label-sm text-label-sm text-primary font-semibold hover:underline active:opacity-75"
        @click="queue.selectAll()"
      >
        全选
      </button>
      <span class="text-outline-variant text-[12px]">|</span>
      <button
        class="font-label-sm text-label-sm text-on-surface-variant active:opacity-75"
        @click="queue.clearSelection()"
      >
        取消
      </button>
    </div>
    <button
      class="flex items-center gap-1 px-space-md py-1.5 rounded-lg font-label-md text-label-md font-semibold shadow-sm active:scale-95 transition-transform"
      :class="
        queue.selected.size > 0
          ? 'bg-error text-on-error'
          : 'bg-error/30 text-on-error/50 cursor-not-allowed'
      "
      :disabled="queue.selected.size === 0"
      @click="queue.removeSelected()"
    >
      <span class="material-symbols-outlined text-[16px]">delete</span>
      <span>批量删除 ({{ queue.selected.size }})</span>
    </button>
  </div>
</template>
