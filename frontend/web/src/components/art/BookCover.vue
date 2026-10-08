<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    hue: number
    title: string
    badge?: string
    footerLeft?: string
    footerRight?: string
  }>(),
  { badge: '', footerLeft: '', footerRight: '' },
)

const coverStyle = computed(() => ({
  background: `linear-gradient(150deg, hsl(${props.hue} 62% 42%) 0%, hsl(${(props.hue + 30) % 360} 70% 24%) 100%)`,
}))

const shortTitle = computed(() => props.title.replace(/[（(].*$/, '').trim().slice(0, 10))
</script>

<template>
  <div class="relative w-full h-full rounded-lg overflow-hidden shadow-md" :style="coverStyle">
    <div class="absolute inset-0 opacity-25">
      <div class="absolute -right-4 -bottom-6">
        <span class="material-symbols-outlined text-white" style="font-size: 96px; font-variation-settings: 'FILL' 1">
          auto_stories
        </span>
      </div>
    </div>
    <div class="relative h-full flex flex-col justify-between p-2">
      <span
        v-if="badge"
        class="self-start bg-white/90 text-on-surface px-1.5 py-0.5 rounded text-[9px] leading-tight font-semibold backdrop-blur-sm max-w-full truncate"
      >
        {{ badge }}
      </span>
      <p class="text-white font-bold text-[12px] leading-snug drop-shadow line-clamp-3">
        {{ shortTitle }}
      </p>
    </div>
    <div
      v-if="footerLeft || footerRight"
      class="absolute bottom-0 inset-x-0 bg-gradient-to-t from-black/70 to-transparent p-1.5 flex items-center justify-between"
    >
      <span class="text-white font-label-sm text-[10px]">{{ footerLeft }}</span>
      <span class="text-white font-label-sm text-[10px]">{{ footerRight }}</span>
    </div>
  </div>
</template>
