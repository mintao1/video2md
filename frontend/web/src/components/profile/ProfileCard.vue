<script setup lang="ts">
import { computed } from 'vue'
import type { Quota, User } from '../../types'

const props = defineProps<{ user: User; quota: Quota | null }>()

const avatarStyle = computed(() => {
  const hue = props.user.avatar_hue
  return { background: `linear-gradient(135deg, hsl(${hue} 70% 62%), hsl(${(hue + 40) % 360} 70% 48%))` }
})

const initial = computed(() => props.user.name[0] ?? '视')
const planLabel = computed(
  () =>
    ({
      free: '免费版',
      pro: '高级学术会员 (Pro)',
      svip: 'SVIP 旗舰会员',
    })[props.user.plan],
)
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
          <div class="flex items-center gap-space-xs mt-1">
            <span
              class="bg-secondary text-on-secondary font-label-sm text-label-sm px-2 py-0.5 rounded-full flex items-center gap-0.5 shadow-sm"
            >
              <span class="material-symbols-outlined text-[12px] filled">workspace_premium</span>
              {{ planLabel }}
            </span>
            <span class="font-body-sm text-body-sm text-outline">{{ user.id_label }}</span>
          </div>
        </div>
      </div>
      <button
        aria-label="编辑资料"
        class="shrink-0 w-9 h-9 rounded-full bg-surface-container hover:bg-surface-container-high text-on-surface flex items-center justify-center transition-transform active:scale-95"
      >
        <span class="material-symbols-outlined text-[18px]">edit</span>
      </button>
    </div>

    <div class="grid grid-cols-3 gap-space-xs pt-space-xs bg-surface-container-low rounded-lg p-space-sm text-center">
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
      <div class="flex flex-col items-center">
        <span class="font-headline-md text-headline-md text-on-surface">
          {{ quota?.stats.accuracy ?? '-' }}<span class="text-[11px] font-normal">%</span>
        </span>
        <span class="font-label-sm text-label-sm text-outline">公式精准度</span>
      </div>
    </div>
  </div>
</template>
