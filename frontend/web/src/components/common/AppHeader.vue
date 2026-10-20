<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useAuthStore } from '../../stores/auth'
import { getToken } from '../../api/client'
import LogoMark from '../art/LogoMark.vue'

defineProps<{ subtitle?: string }>()

const emit = defineEmits<{ (e: 'notifications'): void }>()

const auth = useAuthStore()
const canSignOut = Boolean(getToken())
// Free users get no badge; paid plans show what /me actually reports.
const planBadge = computed(() => ({ free: '', pro: 'Pro 会员', svip: 'SVIP 会员' })[auth.user?.plan ?? 'free'])

onMounted(() => {
  auth.fetchUser().catch(() => {
    /* a 401 is already handled by the API client */
  })
})
const initial = computed(() => auth.user?.name?.[0] ?? '视')
const avatarStyle = computed(() => {
  const hue = auth.user?.avatar_hue ?? 258
  return { background: `linear-gradient(135deg, hsl(${hue} 70% 62%), hsl(${(hue + 40) % 360} 70% 48%))` }
})
</script>

<template>
  <header
    class="fixed top-0 inset-x-0 z-40 pt-safe bg-surface/85 backdrop-blur-xl shadow-[0_1px_8px_rgba(0,0,0,0.04)]"
  >
    <div class="h-16 px-margin flex items-center justify-between max-w-[430px] mx-auto">
      <div class="flex items-center gap-space-sm">
        <LogoMark class="h-8 w-8" />
        <div class="flex flex-col">
          <div class="flex items-center gap-space-xs">
            <span class="font-headline-md text-headline-md tracking-tight text-on-surface">视书</span>
            <span v-if="subtitle" class="font-label-sm text-label-sm text-on-surface-variant font-normal">
              · {{ subtitle }}
            </span>
          </div>
        </div>
      </div>
      <div class="flex items-center gap-space-xs">
        <button
          aria-label="通知"
          class="w-11 h-11 flex items-center justify-center text-on-surface-variant hover:text-on-surface transition-colors"
          @click="emit('notifications')"
        >
          <span class="material-symbols-outlined text-[22px]">notifications</span>
        </button>
        <div
          v-if="planBadge"
          class="flex items-center gap-0.5 px-2 py-0.5 rounded-full bg-primary/10 text-primary font-label-sm text-[11px] leading-tight font-semibold shadow-sm mr-1"
        >
          <span class="material-symbols-outlined text-[13px] filled">workspace_premium</span>
          <span>{{ planBadge }}</span>
        </div>
        <RouterLink
          to="/profile"
          class="flex items-center justify-center w-9 h-9 rounded-full ring-2 ring-primary-fixed text-on-primary font-bold text-[14px] shadow-sm"
          :style="avatarStyle"
        >
          {{ initial }}
        </RouterLink>
        <button v-if="canSignOut" aria-label="退出登录" class="text-xs text-outline" @click="auth.logout">退出</button>
      </div>
    </div>
  </header>
</template>
