<script setup lang="ts">
import { computed } from 'vue'
import type { Quota } from '../../types'
import ProgressBar from '../common/ProgressBar.vue'

const props = defineProps<{ quota: Quota | null }>()

const aiPct = computed(() =>
  props.quota ? Math.round((props.quota.ai_hours_used / props.quota.ai_hours_total) * 100) : 0,
)
const storagePct = computed(() =>
  props.quota ? Math.round((props.quota.storage_gb_used / props.quota.storage_gb_total) * 100) : 0,
)
</script>

<template>
  <div class="flex flex-col gap-space-sm">
    <div class="flex items-center justify-between px-space-xs">
      <span class="font-title-sm text-title-sm text-on-surface">算力与云存储用量</span>
      <span class="font-label-sm text-label-sm text-primary flex items-center gap-0.5 cursor-pointer">
        明细流水
        <span class="material-symbols-outlined text-[14px]">chevron_right</span>
      </span>
    </div>
    <div class="grid grid-cols-2 gap-space-sm">
      <!-- AI 时长 -->
      <div class="bg-surface-container-lowest rounded-xl p-space-md shadow-sm flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="font-label-md text-label-md text-on-surface-variant flex items-center gap-1">
            <span class="material-symbols-outlined text-[16px] text-primary">timelapse</span>
            AI 转换时长
          </span>
          <span class="font-label-sm text-label-sm text-primary bg-primary-fixed px-1.5 py-0.5 rounded-full">
            剩余 {{ 100 - aiPct }}%
          </span>
        </div>
        <div class="my-space-sm flex items-baseline gap-1">
          <span class="font-headline-xl-mobile text-headline-xl-mobile text-on-surface">
            {{ quota?.ai_hours_used ?? '-' }}
          </span>
          <span class="font-body-sm text-body-sm text-outline">/ {{ quota?.ai_hours_total ?? '-' }} 小时</span>
        </div>
        <ProgressBar :value="aiPct" color="bg-primary" height="h-2" />
        <span class="font-body-sm text-[11px] text-outline mt-1.5">重置周期：次月 1 日</span>
      </div>

      <!-- 存储 -->
      <div class="bg-surface-container-lowest rounded-xl p-space-md shadow-sm flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="font-label-md text-label-md text-on-surface-variant flex items-center gap-1">
            <span class="material-symbols-outlined text-[16px] text-secondary">cloud_done</span>
            教材库空间
          </span>
          <span class="font-label-sm text-label-sm text-secondary bg-secondary-fixed px-1.5 py-0.5 rounded-full">
            已用 {{ storagePct }}%
          </span>
        </div>
        <div class="my-space-sm flex items-baseline gap-1">
          <span class="font-headline-xl-mobile text-headline-xl-mobile text-on-surface">
            {{ quota?.storage_gb_used ?? '-' }}
          </span>
          <span class="font-body-sm text-body-sm text-outline">/ {{ quota?.storage_gb_total ?? '-' }} GB</span>
        </div>
        <ProgressBar :value="storagePct" color="bg-secondary-container" height="h-2" />
        <span class="font-body-sm text-[11px] text-outline mt-1.5">云端高清 PDF &amp; Markdown</span>
      </div>
    </div>
  </div>
</template>
