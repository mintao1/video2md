<script setup lang="ts">
import { useRoute } from 'vue-router'
import { computed } from 'vue'
import { useQueueStore } from '../../stores/queue'

const route = useRoute()
const queue = useQueueStore()

const tabs = [
  { path: '/queue', icon: 'motion_photos_on', label: '排队处理' },
  { path: '/library', icon: 'local_library', label: '教材库' },
  { path: '/queue', icon: 'add', label: '新建转换', center: true },
  { path: '/profile', icon: 'person', label: '我的' },
] as const

const badge = computed(() => queue.activeCount)
</script>

<template>
  <nav
    class="fixed bottom-0 inset-x-0 z-40 pb-safe bg-surface/90 backdrop-blur-xl shadow-[0_-2px_12px_rgba(15,23,42,0.06)]"
  >
    <div class="flex justify-around items-center h-16 px-space-xs max-w-[430px] mx-auto">
      <template v-for="(tab, i) in tabs" :key="i">
        <!-- 中央凸起按钮：跳到队列页并预期打开导入面板 -->
        <RouterLink
          v-if="'center' in tab && tab.center"
          :to="{ path: '/queue', query: { openImport: '1' } }"
          class="flex flex-col items-center justify-center -mt-5 min-w-[56px] group"
        >
          <div
            class="w-12 h-12 rounded-full bg-gradient-to-tr from-primary to-secondary flex items-center justify-center text-on-primary shadow-lg shadow-primary/30 transition-transform active:scale-95"
          >
            <span class="material-symbols-outlined text-[26px]">add</span>
          </div>
          <span class="font-label-sm text-label-sm mt-1 text-on-surface font-semibold">{{ tab.label }}</span>
        </RouterLink>
        <RouterLink
          v-else
          :to="tab.path"
          class="flex flex-col items-center justify-center gap-0.5 min-w-[56px] h-11 relative transition-colors"
          :class="
            route.path === tab.path && !(i === 0 && route.query.openImport)
              ? 'text-primary font-title-sm'
              : 'text-on-surface-variant hover:text-on-surface'
          "
        >
          <span class="relative flex items-center justify-center">
            <span class="material-symbols-outlined text-[24px]">{{ tab.icon }}</span>
            <span
              v-if="i === 0 && badge > 0"
              class="absolute -top-1 -right-2 bg-error text-on-error font-label-sm text-[10px] leading-none px-1.5 py-0.5 rounded-full min-w-[16px] text-center"
            >
              {{ badge }}
            </span>
          </span>
          <span class="font-label-sm text-label-sm">{{ tab.label }}</span>
        </RouterLink>
      </template>
    </div>
  </nav>
</template>
