<script setup lang="ts">
import { computed, ref } from 'vue'
import { saveBundle } from '../../download'
import type { RunListItem } from '../../types'
import ProgressBar from '../common/ProgressBar.vue'
import { useQueueStore } from '../../stores/queue'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{
  run: RunListItem
  /** 缩略图渐变封面的色相 */
  hue?: number
}>()

const emit = defineEmits<{ (e: 'read', run: RunListItem): void }>()

const queue = useQueueStore()
const toast = useToastStore()

const busy = ref(false)
async function action(kind: 'cancel' | 'resume' | 'promote' | 'download') {
  if (busy.value) return
  busy.value = true
  try {
    if (kind === 'cancel') await queue.pause(props.run.run_id)
    else if (kind === 'resume') await queue.resume(props.run.run_id)
    else if (kind === 'promote') await queue.promote(props.run.run_id)
    else await saveBundle(props.run.run_id)
    toast.show(
      { cancel: '已提交取消请求', resume: '任务已重新排队', promote: '已设为优先', download: '下载已开始' }[kind],
    )
  } catch (error) {
    toast.show(error instanceof Error ? error.message : '操作失败，请稍后重试')
  } finally {
    busy.value = false
  }
}

const isSelected = computed(() => queue.selected.has(props.run.run_id))

const statusBadge = computed(() => {
  const r = props.run
  switch (r.status) {
    case 'running':
      return { text: '处理中', cls: 'bg-primary/10 text-primary' }
    case 'created':
      return { text: '待处理', cls: 'bg-surface-container text-outline' }
    case 'cancelling':
      return { text: '取消中', cls: 'bg-surface-container text-outline' }
    case 'paused':
      return { text: '已暂停', cls: 'bg-surface-container text-on-surface-variant' }
    case 'queued':
      return { text: `排队第 ${r.queue_pos ?? '?'} 位`, cls: 'bg-tertiary-fixed text-on-tertiary-fixed-variant' }
    case 'done':
      return { text: '转换完成', cls: 'bg-surface-container text-primary' }
    case 'partial':
      return { text: r.readable ? '部分完成' : '生成受阻', cls: 'bg-tertiary-fixed text-on-tertiary-fixed-variant' }
    case 'error':
      return { text: '转换失败', cls: 'bg-error-container text-on-error-container' }
    default:
      return { text: r.status, cls: 'bg-surface-container text-outline' }
  }
})

const thumbStyle = computed(() => ({
  background: `linear-gradient(140deg, hsl(${props.hue ?? 220} 55% 45%), hsl(${(props.hue ?? 220) + 40} 60% 28%))`,
}))

function fmtEta(sec?: number): string {
  if (sec === undefined) return ''
  if (sec >= 60) return `还需 ${Math.ceil(sec / 60)} 分钟`
  return `还需 ${sec} 秒`
}
</script>

<template>
  <div
    class="rounded-xl p-space-md shadow-sm flex flex-col gap-space-md bg-surface-container-lowest"
    :class="isSelected ? 'ring-2 ring-primary' : ''"
  >
    <div class="flex gap-space-md items-start">
      <!-- 批量选择框 -->
      <div v-if="queue.batchMode" class="flex items-center self-center shrink-0">
        <button
          class="w-5 h-5 rounded-md flex items-center justify-center cursor-pointer shadow-sm active:scale-95 transition-all"
          :class="
            isSelected
              ? 'bg-primary text-on-primary'
              : 'border-2 border-outline-variant bg-surface-container-lowest hover:border-primary'
          "
          @click="queue.toggleSelect(run.run_id)"
        >
          <span v-if="isSelected" class="material-symbols-outlined text-[16px] font-semibold leading-none">
            check
          </span>
        </button>
      </div>

      <!-- 缩略图占位 -->
      <div class="relative w-24 h-16 rounded-lg overflow-hidden shrink-0" :style="thumbStyle">
        <span class="material-symbols-outlined absolute inset-0 m-auto text-white/40" style="font-size: 32px">
          smart_display
        </span>
        <span
          v-if="run.duration_label"
          class="absolute bottom-1 right-1 bg-inverse-surface/80 text-inverse-on-surface text-[10px] font-label-sm px-1 rounded"
        >
          {{ run.duration_label }}
        </span>
      </div>

      <!-- 信息 -->
      <div class="flex flex-col flex-1 min-w-0">
        <div class="flex items-center justify-between gap-space-xs">
          <span class="px-1.5 py-0.5 rounded font-label-sm text-[10px] shrink-0" :class="statusBadge.cls">
            {{ statusBadge.text }}
          </span>
          <span
            v-if="run.status === 'running' && run.eta_sec"
            class="font-label-sm text-label-sm text-primary flex items-center gap-0.5 shrink-0"
          >
            <span class="material-symbols-outlined text-[14px]">timer</span>
            {{ fmtEta(run.eta_sec) }}
          </span>
        </div>
        <h3 class="font-title-sm text-title-sm text-on-surface mt-1 truncate">
          {{ run.title }}
        </h3>
        <div
          v-if="run.source_label || run.quality_label"
          class="flex items-center gap-space-sm mt-1 text-on-surface-variant font-label-sm text-[11px]"
        >
          <span v-if="run.source_label" class="flex items-center gap-0.5">
            <span class="material-symbols-outlined text-[13px] text-outline">link</span>
            {{ run.source_label }}
          </span>
          <span v-if="run.quality_label">· {{ run.quality_label }}</span>
        </div>
      </div>
    </div>

    <div class="text-label-sm text-outline break-all">任务编号：{{ run.run_id }}</div>
    <div v-if="run.usage" class="text-body-sm text-on-surface-variant flex flex-wrap gap-space-sm">
      <span>输入 {{ run.usage.llm_in_tok }} tokens</span>
      <span>输出 {{ run.usage.llm_out_tok }} tokens</span>
      <span>GPU {{ run.usage.gpu_s.toFixed(3) }} 秒</span>
      <span>费用 {{ run.cost_usd == null ? '待结算' : `$${run.cost_usd.toFixed(6)}` }}</span>
    </div>
    <div
      v-if="run.diagnostic"
      role="alert"
      class="bg-error-container/50 rounded-lg px-space-sm py-1.5 text-body-sm text-on-error-container"
    >
      <span>失败阶段：{{ run.diagnostic.stage }}。</span>{{ run.diagnostic.message }}
    </div>
    <!-- 进度条（running） -->
    <ProgressBar
      v-if="run.status === 'running' || run.status === 'cancelling'"
      :value="run.progress"
      color="bg-gradient-to-r from-primary to-secondary"
      height="h-1"
    />

    <!-- 错误信息 -->
    <div
      v-if="!run.diagnostic && run.error && (run.status === 'error' || run.status === 'partial')"
      class="bg-error-container/50 rounded-lg px-space-sm py-1.5 font-body-sm text-body-sm text-on-error-container"
    >
      {{ run.error.message }}
    </div>

    <!-- 底部操作行 -->
    <div v-if="run.status === 'running'" class="flex items-center justify-end gap-space-sm pt-1">
      <button
        class="px-space-md py-1.5 rounded-lg bg-surface-container text-on-surface-variant font-label-md text-label-md active:scale-95 transition-transform flex items-center gap-1"
        :disabled="busy"
        @click="action('cancel')"
      >
        <span class="material-symbols-outlined text-[16px]">cancel</span>
        取消任务
      </button>
    </div>

    <div v-else-if="run.status === 'cancelling'" class="text-outline font-label-sm text-label-sm">
      正在等待当前处理阶段结束
    </div>

    <div
      v-else-if="run.status === 'paused' || run.status === 'error'"
      class="flex items-center justify-between bg-surface-container-low px-space-md py-space-sm rounded-lg"
    >
      <div class="flex items-center gap-space-xs text-on-surface-variant font-label-sm text-label-sm">
        <span class="material-symbols-outlined text-[18px] text-outline">pause_circle</span>
        <span>任务已结束，可重新排队</span>
      </div>
      <button
        class="font-label-sm text-label-sm text-primary font-semibold active:opacity-75"
        :disabled="busy"
        @click="action('resume')"
      >
        重试任务
      </button>
    </div>

    <div
      v-else-if="run.status === 'queued'"
      class="flex items-center justify-between bg-surface-container-low px-space-md py-space-sm rounded-lg"
    >
      <div class="flex items-center gap-space-xs text-on-surface-variant font-label-sm text-label-sm">
        <span class="material-symbols-outlined text-[18px] text-outline">hourglass_top</span>
        <span>等待中</span>
      </div>
      <div class="flex items-center gap-3">
        <button class="font-label-sm text-label-sm text-outline" :disabled="busy" @click="action('cancel')">
          取消排队
        </button>
        <button
          class="font-label-sm text-label-sm text-primary font-semibold active:opacity-75"
          :disabled="busy"
          @click="action('promote')"
        >
          设为优先
        </button>
      </div>
    </div>

    <div v-else-if="run.status === 'done' || run.status === 'partial'" class="flex items-center gap-space-sm pt-1">
      <button
        v-if="run.readable"
        class="flex-1 py-2 rounded-lg bg-secondary text-on-secondary font-label-md text-label-md active:scale-95 transition-transform flex items-center justify-center gap-1 shadow-sm"
        @click="emit('read', run)"
      >
        <span class="material-symbols-outlined text-[18px]">menu_book</span>
        立即阅读
      </button>
      <span v-else class="text-body-sm text-outline">尚未生成可读教材</span>
      <button
        v-if="run.status === 'partial'"
        class="font-label-sm text-label-sm text-primary"
        :disabled="busy"
        @click="action('resume')"
      >
        补跑失败条目
      </button>
      <button
        v-if="run.bundle_ready"
        class="px-space-md py-2 rounded-lg bg-surface-container text-on-surface-variant font-label-md text-label-md active:scale-95 transition-transform flex items-center gap-1"
        :disabled="busy"
        @click="action('download')"
      >
        <span class="material-symbols-outlined text-[18px]">download</span>
        下载
      </button>
    </div>
  </div>
</template>
