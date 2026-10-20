<script setup lang="ts">
import ToastHost from './components/common/ToastHost.vue'
</script>

<template>
  <RouterView v-slot="{ Component }">
    <Suspense>
      <component :is="Component" :key="$route.fullPath" />
      <template #fallback>
        <main class="min-h-screen bg-surface flex items-center justify-center text-outline">正在加载…</main>
      </template>
    </Suspense>
  </RouterView>
  <ToastHost />
</template>
