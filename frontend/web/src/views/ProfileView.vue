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
import SvipPromoBanner from '../components/profile/SvipPromoBanner.vue'
import VipMembershipSheet from '../components/profile/VipMembershipSheet.vue'

const auth = useAuthStore()
const profile = useProfileStore()
const toast = useToastStore()

const vipOpen = ref(false)
const billingOpen = ref(false)
const billing = ref<import('../types').CostReport | null>(null)
const billingLoading = ref(false)
const billingError = ref('')
async function toggleBilling() {
  billingOpen.value = !billingOpen.value
  if (!billingOpen.value) return
  billingLoading.value = true
  billingError.value = ''
  try { billing.value = await import('../api').then((api) => api.getCost()) }
  catch { billingError.value = '费用明细加载失败，请检查网络后重新打开。' }
  finally { billingLoading.value = false }
}
// AUTH=off development has no token and nothing to sign out of.
const canSignOut = Boolean(getToken())

// Production must never render a fabricated identity while /me is loading or
// unavailable. Keep the real quota/billing controls, but leave the profile
// card absent until the authenticated user has been fetched.
const user = computed(() => auth.user)

onMounted(async () => {
  try {
    await auth.fetchUser()
    await profile.load()
  } catch {
    toast.show('个人资料暂时无法加载，请检查网络后重试')
  }
})

function onNotionToggle() {
  profile.setNotionSync(!profile.notionSync)
  toast.show(profile.notionSync ? '已开启：生成完成后自动同步 Notion' : '已关闭 Notion 自动同步')
}
</script>

<template>
  <div class="min-h-screen bg-surface">
    <main class="flex flex-col relative w-full pt-16 pb-nav bg-surface min-h-screen">
      <div class="w-full px-margin pt-space-md pb-space-lg flex flex-col gap-space-lg">
        <!-- 资料卡 -->
        <ProfileCard v-if="user" :user="user" :quota="profile.quota" />

        <RouterLink to="/profile/colab" class="bg-surface-container-lowest rounded-xl shadow-sm p-space-md flex items-center justify-between gap-space-md hover:bg-surface-container-low transition-colors">
          <div class="flex items-center gap-space-md">
            <span class="material-symbols-outlined text-primary" aria-hidden="true">cloud_sync</span>
            <div class="flex flex-col">
              <span class="font-title-sm text-title-sm text-on-surface">Colab 授权</span>
              <span class="text-body-sm text-outline">连接 Google 账号，启用云端转写与剪辑</span>
            </div>
          </div>
          <span class="material-symbols-outlined text-outline" aria-hidden="true">chevron_right</span>
        </RouterLink>

        <a href="https://www.kaggle.com/account/login" target="_blank" rel="noopener noreferrer"
          class="bg-surface-container-lowest rounded-xl shadow-sm p-space-md flex items-center justify-between gap-space-md hover:bg-surface-container-low transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary">
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
          <SvipPromoBanner v-if="user" :plan="user.plan" @upgrade="vipOpen = true" />
        </div>

        <!-- 排版与引擎配置 -->
        <LlmSettingsCard />

        <div class="flex flex-col gap-space-sm">
          <span class="font-title-sm text-title-sm text-on-surface px-space-xs">转书排版与 AI 引擎配置</span>
          <div class="bg-surface-container-lowest rounded-xl shadow-sm overflow-hidden flex flex-col">
            <button class="p-space-md flex items-center justify-between hover:bg-surface-container-low transition-colors text-left">
              <div class="flex items-center gap-space-md min-w-0">
                <div class="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-primary shrink-0">
                  <span class="material-symbols-outlined text-[20px]">auto_stories</span>
                </div>
                <div class="flex flex-col min-w-0">
                  <span class="font-title-sm text-title-sm text-on-surface">默认教材排版模板</span>
                  <span class="font-body-sm text-body-sm text-outline truncate">学术专著风格 · 自动目录与摘要</span>
                </div>
              </div>
              <div class="flex items-center gap-space-xs shrink-0">
                <span class="font-label-md text-label-md text-on-surface-variant">学术专著</span>
                <span class="material-symbols-outlined text-[18px] text-outline">chevron_right</span>
              </div>
            </button>
            <div class="h-[1px] bg-surface-container mx-space-md" />
            <button class="p-space-md flex items-center justify-between hover:bg-surface-container-low transition-colors text-left">
              <div class="flex items-center gap-space-md min-w-0">
                <div class="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-tertiary-container shrink-0">
                  <span class="material-symbols-outlined text-[20px]">ios_share</span>
                </div>
                <div class="flex flex-col min-w-0">
                  <span class="font-title-sm text-title-sm text-on-surface">默认导出格式与备份</span>
                  <span class="font-body-sm text-body-sm text-outline truncate">PDF 打印版 / EPUB / Markdown 原始包</span>
                </div>
              </div>
              <div class="flex items-center gap-space-xs shrink-0">
                <span class="font-label-md text-label-md text-on-surface-variant">PDF + MD</span>
                <span class="material-symbols-outlined text-[18px] text-outline">chevron_right</span>
              </div>
            </button>
            <div class="h-[1px] bg-surface-container mx-space-md" />
            <div class="p-space-md flex items-center justify-between">
              <div class="flex items-center gap-space-md min-w-0">
                <div class="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-primary shrink-0">
                  <span class="material-symbols-outlined text-[20px]">sync_saved_locally</span>
                </div>
                <div class="flex flex-col min-w-0">
                  <span class="font-title-sm text-title-sm text-on-surface">生成完成后自动同步 Notion</span>
                  <span class="font-body-sm text-body-sm text-outline truncate">推送到《我的视频教材知识库》页面</span>
                </div>
              </div>
              <button
                role="switch"
                :aria-checked="profile.notionSync"
                class="shrink-0 w-11 h-6 rounded-full relative p-0.5 transition-colors focus:outline-none"
                :class="profile.notionSync ? 'bg-primary' : 'bg-surface-container-highest'"
                @click="onNotionToggle"
              >
                <span
                  class="block w-5 h-5 bg-surface-container-lowest rounded-full shadow-sm transition-transform"
                  :class="profile.notionSync ? 'translate-x-5' : 'translate-x-0.5'"
                />
              </button>
            </div>
          </div>
        </div>

        <!-- 支持与系统 -->
        <div class="flex flex-col gap-space-sm">
          <span class="font-title-sm text-title-sm text-on-surface px-space-xs">支持与系统信息</span>
          <div class="bg-surface-container-lowest rounded-xl shadow-sm overflow-hidden flex flex-col">
            <button class="p-space-md flex items-center justify-between hover:bg-surface-container-low transition-colors text-left">
              <div class="flex items-center gap-space-md">
                <div class="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-on-surface">
                  <span class="material-symbols-outlined text-[20px]">help_outline</span>
                </div>
                <div class="flex flex-col">
                  <span class="font-title-sm text-title-sm text-on-surface">常见问题解答与工单反馈</span>
                  <span class="font-body-sm text-body-sm text-outline">遇到排队超时或公式解析错误？</span>
                </div>
              </div>
              <span class="material-symbols-outlined text-[18px] text-outline">chevron_right</span>
            </button>
            <div class="h-[1px] bg-surface-container mx-space-md" />
            <button
              class="p-space-md flex items-center justify-between hover:bg-surface-container-low transition-colors text-left"
              @click="toast.show('视书 v0.1.0 · 基于 video2pdf 后端')"
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

    <VipMembershipSheet v-model="vipOpen" @pay="(cycle) => toast.show(`模拟支付：${cycle} 周期 SVIP（mock，不产生真实扣款）`)" />
    <BottomNav />
  </div>
</template>
