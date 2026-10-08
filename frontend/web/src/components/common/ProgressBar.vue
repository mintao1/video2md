<script setup lang="ts">
withDefaults(
  defineProps<{
    value: number
    color?: string
    height?: string
  }>(),
  { color: 'bg-primary', height: 'h-1.5' },
)
</script>

<template>
  <div class="w-full bg-surface-container rounded-full overflow-hidden" :class="height">
    <div
      class="h-full rounded-full transition-all duration-500"
      :class="color"
      :style="{ width: `${Math.min(100, Math.max(0, value))}%` }"
    />
  </div>
</template>
