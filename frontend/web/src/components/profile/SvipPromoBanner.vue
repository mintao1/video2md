<script setup lang="ts">
const emit = defineEmits<{ (e: 'upgrade'): void }>()
</script>

<template>
  <div
    class="bg-gradient-to-r from-on-surface to-inverse-surface rounded-xl p-space-md text-inverse-on-surface shadow-md flex items-center justify-between relative overflow-hidden mt-space-xs"
  >
    <div class="absolute -right-6 -bottom-6 w-24 h-24 bg-primary/20 rounded-full blur-xl pointer-events-none" />
    <div class="flex items-center gap-space-sm z-10 min-w-0 pr-space-xs">
      <div class="w-10 h-10 rounded-lg bg-tertiary-fixed-dim/20 flex items-center justify-center shrink-0">
        <span class="material-symbols-outlined text-tertiary-fixed text-[24px] filled">auto_awesome</span>
      </div>
      <div class="flex flex-col min-w-0">
        <div class="flex items-center gap-1">
          <span class="font-title-sm text-title-sm text-tertiary-fixed font-bold">SVIP 旗舰学术特权</span>
          <span class="bg-tertiary text-on-tertiary font-label-sm text-[10px] px-1.5 py-0.5 rounded">旗舰版</span>
        </div>
        <span class="font-body-sm text-body-sm text-surface-variant/80 truncate">
          享 4K 超清板书提纯 &amp; 独立专属 GPU 队列
        </span>
      </div>
    </div>
    <button
      class="shrink-0 bg-tertiary-fixed text-on-tertiary-fixed font-title-sm text-title-sm px-3.5 py-1.5 rounded-lg shadow-sm hover:brightness-105 active:scale-95 transition-all z-10"
      @click="emit('upgrade')"
    >
      立即续费
    </button>
  </div>
</template>
