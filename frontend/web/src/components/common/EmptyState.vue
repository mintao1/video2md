<script setup lang="ts">
withDefaults(
  defineProps<{
    icon?: string
    title: string
    description?: string
    actionLabel?: string
  }>(),
  { icon: 'manage_search', description: '', actionLabel: '' },
)

const emit = defineEmits<{ (e: 'action'): void }>()
</script>

<template>
  <div class="flex flex-col items-center justify-center py-12 px-margin text-center">
    <div class="w-16 h-16 rounded-full bg-surface-container flex items-center justify-center text-outline mb-3">
      <span class="material-symbols-outlined text-[32px]">{{ icon }}</span>
    </div>
    <h4 class="font-headline-md text-headline-md text-on-surface">{{ title }}</h4>
    <p v-if="description" class="font-body-sm text-body-sm text-on-surface-variant max-w-xs mt-1">
      {{ description }}
    </p>
    <button
      v-if="actionLabel"
      class="mt-4 px-4 py-2 rounded-lg bg-primary text-on-primary font-label-md text-label-md shadow-sm active:scale-95 transition-transform"
      @click="emit('action')"
    >
      {{ actionLabel }}
    </button>
  </div>
</template>
