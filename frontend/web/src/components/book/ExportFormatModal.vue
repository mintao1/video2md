<script setup lang="ts">
import BottomSheet from '../common/BottomSheet.vue'

defineProps<{ modelValue: boolean; bookTitle: string }>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'export', format: string): void
}>()

const formats = [
  { key: 'PDF', icon: 'picture_as_pdf', iconCls: 'text-error', label: '出版级 PDF', hint: '含矢量公式与超链' },
  { key: 'Markdown', icon: 'markdown', iconCls: 'text-primary', label: 'Markdown (.md)', hint: '适配 Obsidian/Notion' },
  { key: 'EPUB', icon: 'book', iconCls: 'text-secondary', label: '电子书 EPUB', hint: '支持微信读书/Kindle' },
  { key: 'LaTeX', icon: 'code', iconCls: 'text-tertiary', label: 'LaTeX 源码包', hint: '含 Overleaf 模板' },
]
</script>

<template>
  <BottomSheet
    :model-value="modelValue"
    icon="ios_share"
    title="导出教材"
    :subtitle="`《${bookTitle}》导出参数确认`"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="grid grid-cols-2 gap-2 pb-2">
      <button
        v-for="f in formats"
        :key="f.key"
        class="p-3 rounded-lg bg-surface-container-low hover:bg-surface-container-high transition-colors flex flex-col items-center gap-1.5 text-center active:scale-95"
        @click="emit('export', f.key), emit('update:modelValue', false)"
      >
        <span class="material-symbols-outlined text-[28px]" :class="f.iconCls">{{ f.icon }}</span>
        <span class="font-title-sm text-label-md text-on-surface">{{ f.label }}</span>
        <span class="font-label-sm text-[10px] text-outline">{{ f.hint }}</span>
      </button>
    </div>
  </BottomSheet>
</template>
