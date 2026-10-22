<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { request, safeRedirect } from '../api/client'
import BottomNav from '../components/common/BottomNav.vue'

type Status = { can_authorize: boolean; has_credentials: boolean }
const status = ref<Status | null>(null)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const message = ref('')
const flowId = ref('')
const authorizationUrl = ref('')
const code = ref('')
const route = useRoute()
const next = computed(() => safeRedirect(route.query.next))
const onboarding = computed(() => typeof route.query.next === 'string')

async function load() {
  loading.value = true
  error.value = ''
  try {
    status.value = await request<Status>('/settings/colab')
  } catch {
    error.value = '无法读取授权状态，请稍后重试。'
  } finally {
    loading.value = false
  }
}

async function start() {
  if (busy.value) return
  // Open synchronously from the click so browsers don't block the OAuth tab.
  const authTab = window.open('about:blank', '_blank')
  if (authTab) authTab.opener = null
  busy.value = true
  error.value = ''
  message.value = ''
  flowId.value = ''
  authorizationUrl.value = ''
  code.value = ''
  try {
    const result = await request<{ flow_id: string; authorization_url: string }>('/settings/colab/start', {
      method: 'POST',
    })
    const url = new URL(result.authorization_url)
    if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com') throw new Error('Invalid OAuth URL')
    flowId.value = result.flow_id
    authorizationUrl.value = url.href
    // A popup navigation can fail independently (embedded browsers often block it).
    // Keep the flow state and expose the fallback link instead of treating that as
    // an OAuth start failure.
    if (authTab) {
      try {
        authTab.location.replace(url.href)
      } catch {
        authTab.close()
      }
    }
  } catch (err) {
    authTab?.close()
    error.value = err instanceof Error ? err.message : '无法开始授权，请重试。'
  } finally {
    busy.value = false
  }
}

async function complete() {
  if (busy.value || !code.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    status.value = await request<Status>('/settings/colab/complete', {
      method: 'POST',
      body: JSON.stringify({ flow_id: flowId.value, code: code.value.trim() }),
    })
    message.value = '你的 Colab 授权已保存，仅用于当前账号后续的转换任务。'
    flowId.value = ''
    authorizationUrl.value = ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : '授权失败，请重新开始。'
    // The backend consumes each exchange; errors require a fresh authorization.
    flowId.value = ''
    authorizationUrl.value = ''
  } finally {
    code.value = ''
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="min-h-screen bg-surface text-on-surface">
    <main class="w-full max-w-xl mx-auto px-margin pt-16 pb-nav flex flex-col gap-space-lg">
      <RouterLink to="/profile" class="flex items-center gap-space-xs text-primary self-start">
        <span class="material-symbols-outlined" aria-hidden="true">arrow_back</span>返回我的
      </RouterLink>
      <h1 class="font-title-lg text-title-lg">Colab 授权</h1>
      <p v-if="onboarding" class="text-body-sm text-on-surface-variant">
        Google 登录成功，连接自己的 Colab 后即可使用个人算力。
      </p>
      <section
        class="bg-surface-container-lowest rounded-xl shadow-sm p-space-lg flex flex-col gap-space-md"
        :aria-busy="loading || busy"
      >
        <p class="text-body-md">连接 Google 账号，用于云端转写、画面识别与视频剪辑。</p>
        <p class="text-body-sm text-on-surface-variant">
          使用与当前登录相同的 Google 账号连接 Colab，授权和算力会话仅用于你的转换任务。
        </p>
        <p class="text-body-sm text-outline">
          首次需要在 Google 页面同意 Colab 权限。连接后，再次登录视书可复用已保存的授权。
        </p>
        <p v-if="loading" role="status">正在读取授权状态…</p>
        <template v-else-if="status">
          <p class="text-body-sm">
            {{
              !status.can_authorize
                ? '请先使用 Google 账号登录'
                : status.has_credentials
                  ? '已连接我的 Colab，可重新授权'
                  : '尚未连接我的 Colab'
            }}
          </p>
          <p v-if="status.has_credentials" class="text-body-sm text-outline">
            凭证有效性和 GPU 配额会在实际连接时检查。
          </p>
          <p v-if="!status.can_authorize" class="text-body-sm text-outline">
            登录后即可连接自己的 Colab，无需管理员授权。
          </p>
          <template v-else>
            <ol class="list-decimal pl-5 text-body-sm flex flex-col gap-space-sm">
              <li>打开 Google 授权页，登录并同意所需权限。</li>
              <li>复制 Google 页面显示的授权码，回到这里提交。授权流程有效期为 10 分钟。</li>
            </ol>
            <button
              type="button"
              :disabled="busy"
              class="rounded-xl bg-primary text-on-primary py-3 px-space-md disabled:opacity-50"
              @click="start"
            >
              {{ busy ? '处理中…' : flowId ? '重新开始授权' : '连接我的 Google Colab' }}
            </button>
            <a
              v-if="authorizationUrl"
              :href="authorizationUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="text-primary underline text-body-sm"
              >如果新窗口未打开，点此打开 Google 授权页</a
            >
            <form v-if="flowId" class="flex flex-col gap-space-sm" @submit.prevent="complete">
              <label for="colab-code" class="text-label-md">Google 授权码</label>
              <input
                id="colab-code"
                v-model="code"
                type="password"
                required
                maxlength="4096"
                autocomplete="off"
                spellcheck="false"
                :disabled="busy"
                placeholder="粘贴 Google 页面显示的授权码"
                class="w-full rounded-lg border border-outline bg-surface p-3"
              />
              <button
                type="submit"
                :disabled="busy || !code.trim()"
                class="rounded-xl bg-primary text-on-primary py-3 disabled:opacity-50"
              >
                提交授权码
              </button>
            </form>
          </template>
        </template>
        <p v-if="error" role="alert" class="text-error text-body-sm">{{ error }}</p>
        <p v-if="message" role="status" class="text-primary text-body-sm">{{ message }}</p>
        <button v-if="!loading && !status" type="button" class="text-primary" @click="load">重新加载</button>
        <RouterLink v-if="onboarding" :to="next" class="text-primary text-center py-2">{{
          status?.has_credentials ? '继续使用' : '稍后连接，继续使用'
        }}</RouterLink>
      </section>
    </main>
    <BottomNav />
  </div>
</template>
