<script setup lang="ts">
import { computed } from 'vue'
import type { Quota, User } from '../../types'

const props = defineProps<{ user: User; quota: Quota | null }>()

const avatarStyle = computed(() => {
  const hue = props.user.avatar_hue
  return { background: `linear-gradient(135deg, hsl(${hue} 70% 62%), hsl(${(hue + 40) % 360} 70% 48%))` }
})

const initial = computed(() => props.user.name[0] ?? '视')
</script>

<template>
  <div class="bg-surface-container-lowest rounded-xl p-space-md shadow-sm flex flex-col gap-space-md">
    <div class="flex items-center justify-between gap-space-md">
      <div class="flex items-center gap-space-md min-w-0">
        <div class="relative shrink-0">
          <div
            class="w-16 h-16 rounded-full shadow-sm ring-2 ring-primary-fixed flex items-center justify-center text-on-primary text-2xl font-bold"
            :style="avatarStyle"
          >
            {{ initial }}
          </div>
          <span
            class="absolute bottom-0 right-0 bg-primary text-on-primary w-5 h-5 rounded-full flex items-center justify-center text-[11px] shadow-sm"
          >
            <span class="material-symbols-outlined text-[13px] filled">verified</span>
          </span>
        </div>
        <div class="flex flex-col min-w-0">
          <span class="font-headline-md text-headline-md text-on-surface truncate">
            {{ user.display_name }}
          </span>
          <span class="font-body-sm text-body-sm text-outline mt-1">{{ user.id_label }}</span>
        </div>
      </div>
    </div>

    <div class="grid grid-cols-2 gap-space-xs pt-space-xs bg-surface-container-low rounded-lg p-space-sm text-center">
      <div class="flex flex-col items-center">
        <span class="font-headline-md text-headline-md text-primary">{{ quota?.stats.books ?? '-' }}</span>
        <span class="font-label-sm text-label-sm text-outline">已生成教材</span>
      </div>
      <div class="flex flex-col items-center">
        <span class="font-headline-md text-headline-md text-secondary">
          {{ quota?.stats.knowledge_points ?? '-' }}
        </span>
        <span class="font-label-sm text-label-sm text-outline">提取知识点</span>
      </div>
    </div>
  </div>
</template>
