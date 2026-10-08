<script setup lang="ts">
export interface ChipOption {
  key: string
  label: string
  icon?: string
  count?: number
}

defineProps<{
  options: ChipOption[]
  modelValue: string
}>()

const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()
</script>

<template>
  <div class="flex items-center gap-space-xs overflow-x-auto no-scrollbar py-1 -mx-margin px-margin">
    <button
      v-for="opt in options"
      :key="opt.key"
      class="flex-shrink-0 px-3.5 py-1.5 rounded-full font-label-md text-label-md transition-all active:scale-95 flex items-center gap-1"
      :class="
        modelValue === opt.key
          ? 'bg-primary text-on-primary shadow-sm'
          : 'bg-surface-container text-on-surface-variant hover:bg-surface-container-high'
      "
      @click="emit('update:modelValue', opt.key)"
    >
      <span v-if="opt.icon" class="material-symbols-outlined text-[14px]">{{ opt.icon }}</span>
      <span>{{ opt.label }}</span>
      <span v-if="opt.count !== undefined">({{ opt.count }})</span>
    </button>
  </div>
</template>
