<script setup lang="ts">
import { watch } from 'vue'

const props = defineProps<{
  modelValue: boolean
  title?: string
  subtitle?: string
  icon?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
}>()

function close() {
  emit('update:modelValue', false)
}

watch(
  () => props.modelValue,
  (open) => {
    document.body.style.overflow = open ? 'hidden' : ''
  },
)
</script>

<template>
  <Teleport to="body">
    <div
      v-if="modelValue"
      class="fixed inset-0 z-50 flex flex-col justify-end animate-fade-in"
      style="max-width: 430px; margin: 0 auto"
    >
      <div class="absolute inset-0 bg-inverse-surface/50 backdrop-blur-sm" @click="close" />
      <div
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        class="relative z-10 w-full bg-surface-container-lowest rounded-t-2xl shadow-2xl animate-sheet-in flex flex-col"
        :class="$slots.footer ? 'max-h-[85vh]' : 'max-h-[75vh]'"
      >
        <div class="pt-3 pb-1 flex flex-col items-center shrink-0">
          <div class="w-10 h-1 bg-outline-variant/80 rounded-full" />
        </div>
        <div v-if="title || $slots.header" class="px-space-md pb-3 flex items-center justify-between shrink-0">
          <slot name="header">
            <div class="flex items-center gap-2 min-w-0">
              <span v-if="icon" class="material-symbols-outlined text-primary text-[22px]">{{ icon }}</span>
              <div class="min-w-0">
                <h3 class="font-headline-md text-headline-md text-on-surface truncate leading-tight">
                  {{ title }}
                </h3>
                <p v-if="subtitle" class="font-label-sm text-label-sm text-outline mt-0.5">
                  {{ subtitle }}
                </p>
              </div>
            </div>
          </slot>
          <button
            aria-label="关闭"
            class="w-8 h-8 shrink-0 rounded-full bg-surface-container flex items-center justify-center text-on-surface-variant active:scale-95 transition-transform"
            @click="close"
          >
            <span class="material-symbols-outlined text-[18px]">close</span>
          </button>
        </div>
        <div class="overflow-y-auto no-scrollbar px-space-md flex-1">
          <slot />
        </div>
        <div v-if="$slots.footer" class="px-space-md pt-3 pb-safe border-t border-surface-container shrink-0">
          <slot name="footer" />
        </div>
      </div>
    </div>
  </Teleport>
</template>
