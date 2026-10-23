<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useProfileStore } from '../stores/profile'
import { useToastStore } from '../stores/toast'
import { getToken } from '../api/client'
import BottomNav from '../components/common/BottomNav.vue'
import ProfileCard from '../components/profile/ProfileCard.vue'
import LlmSettingsCard from '../components/profile/LlmSettingsCard.vue'
import QuotaDashboard from '../components/profile/QuotaDashboard.vue'
import BillingPanel from '../components/profile/BillingPanel.vue'

const auth = useAuthStore()
const profile = useProfileStore()
const toast = useToastStore()

const billingOpen = ref(false)
const billing = ref<import('../types').CostReport | null>(null)
const billingLoading = ref(false)
const billingError = ref('')
async function toggleBilling() {
  billingOpen.value = !billingOpen.value
  if (!billingOpen.value) return
  billingLoading.value = true
  billingError.value = ''
  try {
    billing.value = await import('../api').then((api) => api.getCost())
  } catch {
    billingError.value = '费用明细加载失败，请检查网络后重新打开。'
  } finally {
    billingLoading.value = false
  }
}
// AUTH=off development has no token and nothing to sign out of.
const canSignOut = Boolean(getToken())

const user = computed(
  () =>
    auth.user ?? {
      uid: '',
      name: '视书用户',
      display_name: '视书用户',
      avatar_hue: 258,
      plan: 'free' as const,
      id_label: 'ID: ----',
    },
)

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
        <ProfileCard :user="user" :quota="profile.quota" />

        <RouterLink
          to="/profile/colab"
          class="bg-surface-container-lowest rounded-xl shadow-sm p-space-md flex items-center justify-between gap-space-md hover:bg-surface-container-low transition-colors"
        >
          <div class="flex items-center gap-space-md">
            <span class="material-symbols-outlined text-primary" aria-hidden="true">cloud_sync</span>
            <div class="flex flex-col">
              <span class="font-title-sm text-title-sm text-on-surface">Colab 授权</span>
              <span class="text-body-sm text-outline">连接 Google 账号，启用云端转写与剪辑</span>
            </div>
          </div>
          <span class="material-symbols-outlined text-outline" aria-hidden="true">chevron_right</span>
        </RouterLink>

        <a
          href="https://www.kaggle.com/account/login"
          target="_blank"
          rel="noopener noreferrer"
          class="bg-surface-container-lowest rounded-xl shadow-sm p-space-md flex items-center justify-between gap-space-md hover:bg-surface-container-low transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          <div class="flex items-center gap-space-md min-w-0">
            <span class="material-symbols-outlined text-primary shrink-0" aria-hidden="true">login</span>
            <div class="flex flex-col min-w-0">
              <span class="font-title-sm text-title-sm text-on-surface">Kaggle 登录</span>
              <span class="text-body-sm text-outline">前往 Kaggle 官网登录（新标签页）</span>
            </div>
          </div>
          <span class="material-symbols-outlined text-outline shrink-0" aria-hidden="true">open_in_new</span>
        </a>

        <!-- 配额 -->
        <div class="flex flex-col gap-space-sm">
          <QuotaDashboard :quota="profile.quota" @details="toggleBilling" />
          <BillingPanel v-if="billingOpen" :report="billing" :loading="billingLoading" :error="billingError" />
        </div>

        <!-- AI 引擎配置 -->
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
