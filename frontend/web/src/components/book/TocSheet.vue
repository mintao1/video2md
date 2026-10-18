<script setup lang="ts">
import { ref, watch } from 'vue'
import BottomSheet from '../common/BottomSheet.vue'
import type { TocChapter } from '../../types'

const props = defineProps<{
  modelValue: boolean
  bookTitle: string
  chapters: TocChapter[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'openChapter', ch: TocChapter): void
}>()

const selected = ref<number | null>(null)

watch(
  () => props.modelValue,
  (open) => {
    if (open) selected.value = props.chapters.find((c) => c.state === 'current')?.no ?? null
  },
)

function stateIcon(state: TocChapter['state']) {
  switch (state) {
    case 'done':
      return { icon: 'check_circle', cls: 'text-emerald-600' }
    case 'current':
      return null
    case 'locked':
      return { icon: 'lock_clock', cls: 'text-outline' }
    case 'quiz':
      return { icon: 'quiz', cls: 'text-outline' }
  }
}
</script>

<template>
  <BottomSheet
    :model-value="modelValue"
    icon="auto_stories"
    :title="`《${bookTitle}》课件大纲`"
    subtitle="已提取对应视频分P与关键帧时段"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="flex flex-col gap-2 py-2">
      <button
        v-for="ch in chapters"
        :key="ch.no"
        class="p-3 rounded-lg flex items-center justify-between text-left transition-colors"
        :class="selected === ch.no ? 'bg-surface-container' : 'bg-surface-container-low'"
        @click="selected = ch.no"
      >
        <div class="flex items-center gap-2.5 min-w-0">
          <span
            class="w-6 h-6 shrink-0 rounded-full font-label-sm text-label-sm flex items-center justify-center font-bold"
            :class="
              selected === ch.no
                ? 'bg-primary text-on-primary'
                : 'bg-surface-container-highest text-on-surface-variant'
            "
          >
            {{ ch.no }}
          </span>
          <div class="flex flex-col min-w-0">
            <span class="font-title-sm text-title-sm text-on-surface truncate">{{ ch.title }}</span>
            <span class="font-label-sm text-label-sm text-outline">{{ ch.meta }}</span>
          </div>
        </div>
        <span
          v-if="selected === ch.no && ch.state === 'current'"
          class="shrink-0 px-2 py-0.5 rounded-full bg-primary/10 text-primary font-label-sm text-[10px]"
        >
          进行中
        </span>
        <span
          v-else-if="stateIcon(ch.state)"
          class="material-symbols-outlined text-[18px] shrink-0"
          :class="stateIcon(ch.state)!.cls"
        >
          {{ stateIcon(ch.state)!.icon }}
        </span>
      </button>
    </div>

    <template #footer>
      <button
        class="w-full py-2.5 rounded-lg bg-primary text-on-primary font-title-sm text-title-sm flex items-center justify-center gap-1 shadow-sm active:scale-95 transition-transform"
        @click="
          selected !== null && emit('openChapter', chapters.find((c) => c.no === selected)!),
            emit('update:modelValue', false)
        "
      >
        <span>定位并打开所选章节</span>
        <span class="material-symbols-outlined text-[18px]">arrow_forward</span>
      </button>
    </template>
  </BottomSheet>
</template>
