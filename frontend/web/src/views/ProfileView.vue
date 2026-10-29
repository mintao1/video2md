<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useProfileStore } from '../stores/profile'
import { useToastStore } from '../stores/toast'
import { getToken } from '../api/client'
import BottomNav from '../components/common/BottomNav.vue'
import ProfileCard from '../components/profile/ProfileCard.vue'
import LlmSettingsCard from '../components/profile/LlmSettingsCard.vue'

const auth = useAuthStore()
const profile = useProfileStore()
const toast = useToastStore()

// AUTH=off development has no token and nothing to sign out of.
const canSignOut = Boolean(getToken())

const user = computed(() => auth.user)

onMounted(async () => {
  try {
    await auth.fetchUser()
    await profile.load()
  } catch {
    toast.show('个人资料暂时无法加载，请检查网络后重试')
  }
})

</script>

<template>
  <div class="min-h-screen bg-surface">
    <main class="flex flex-col relative w-full pt-16 pb-nav bg-surface min-h-screen">
      <div class="w-full px-margin pt-space-md pb-space-lg flex flex-col gap-space-lg">
        <!-- 资料卡 -->
        <ProfileCard v-if="user" :user="user" :quota="profile.quota" />

        <!-- 排版与引擎配置 -->
        <LlmSettingsCard />

        <!-- 支持与系统 -->
        <div class="flex flex-col gap-space-sm">
          <span class="font-title-sm text-title-sm text-on-surface px-space-xs">支持与系统信息</span>
          <div class="bg-surface-container-lowest rounded-xl shadow-sm overflow-hidden flex flex-col">
            <button
              class="p-space-md flex items-center justify-between hover:bg-surface-container-low transition-colors text-left"
              @click="toast.show('视书 v0.1.0 · 基于 video2md 后端')"
            >
              <div class="flex items-center gap-space-md">
                <div class="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-outline">
                  <span class="material-symbols-outlined text-[20px]">info</span>
                </div>
                <div class="flex flex-col">
                  <span class="font-title-sm text-title-sm text-on-surface">关于视书</span>
                  <span class="font-body-sm text-body-sm text-outline">新特性与版本更新说明</span>
                </div>
              </div>
              <div class="flex items-center gap-space-xs">
                <span class="font-label-sm text-label-sm text-outline">v0.1.0</span>
                <span class="material-symbols-outlined text-[18px] text-outline">chevron_right</span>
              </div>
            </button>
          </div>
        </div>

        <button
          v-if="canSignOut"
          class="w-full rounded-xl bg-surface-container-lowest shadow-sm p-space-md text-center font-title-sm text-title-sm text-error hover:bg-surface-container-low transition-colors"
          @click="auth.logout"
        >
          退出登录
        </button>
      </div>
    </main>

    <BottomNav />
  </div>
</template>
