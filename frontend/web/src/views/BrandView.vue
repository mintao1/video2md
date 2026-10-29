<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { BASE_URL, request, safeRedirect } from '../api/client'
import LogoMark from '../components/art/LogoMark.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const agreed = ref(false)
const error = ref('')
const busy = ref(false)
const redirect = computed(() => safeRedirect(route.query.redirect))
const notice = computed(() => (route.query.reason === 'expired' ? '登录已过期，请在品牌首页重新登录' : ''))

onMounted(async () => {
  const fragment = new URLSearchParams(window.location.hash.slice(1))
  const token = fragment.get('id_token')
  const authError = fragment.get('auth_error')
  window.history.replaceState({}, document.title, `${window.location.pathname}${window.location.search}`)
  if (token) {
    auth.acceptToken(token)
    busy.value = true
    let destination = redirect.value
    try {
      const colab = await request<{ can_authorize: boolean; has_credentials: boolean }>('/settings/colab')
      if (colab.can_authorize && !colab.has_credentials && !destination.startsWith('/profile/colab')) {
        destination = `/profile/colab?${new URLSearchParams({ next: destination })}`
      }
    } catch {
      // A temporary Colab status failure must not prevent website sign-in.
    }
    await router.replace(destination)
  } else if (authError) {
    error.value = authError
  }
})

function startGoogleLogin(): void {
  if (!agreed.value) {
    error.value = '请先阅读并同意《用户服务协议》与《隐私保护政策》'
    return
  }
  error.value = ''
  busy.value = true
  const params = new URLSearchParams({ terms_accepted: '1' })
  if (redirect.value !== '/library') params.set('redirect', redirect.value)
  window.location.assign(`${BASE_URL}/auth/google/login?${params}`)
}
</script>

<template>
  <main class="flex min-h-screen w-full flex-col bg-surface px-5 pb-safe pt-safe">
    <section class="mx-auto flex w-full max-w-md flex-1 flex-col justify-center py-10">
      <header class="mb-8 flex flex-col items-center text-center">
        <LogoMark class="mb-4 h-20 w-20 !rounded-2xl shadow-md" />
        <h1 class="font-headline-lg-mobile text-headline-lg-mobile tracking-tight text-on-surface">视书</h1>
        <p class="mt-2 font-body-sm text-body-sm text-outline">视频一键转成可精读的教材</p>
        <p class="mt-3 max-w-sm text-sm leading-relaxed text-outline">从视频到结构化知识，让每一次观看都沉淀为可搜索、可复习、可分享的内容。</p>
      </header>

      <div class="rounded-2xl bg-surface-container-lowest p-6 shadow-md">
        <div class="text-center">
          <h2 class="text-xl font-bold text-on-surface">开始使用视书</h2>
          <p class="mt-2 text-sm text-outline">登录后创建教材、管理任务并继续阅读</p>
        </div>

        <p v-if="notice && !error" class="mt-5 rounded-xl bg-surface-container p-3 text-sm text-on-surface">
          {{ notice }}
        </p>
        <p v-if="error" role="alert" class="mt-5 rounded-xl bg-red-50 p-3 text-sm text-red-700">
          {{ error }}
        </p>

        <button
          class="mt-6 flex w-full items-center justify-center gap-3 rounded-xl bg-white px-4 py-3 font-semibold text-on-surface shadow-sm ring-1 ring-outline-variant transition hover:bg-surface active:scale-[0.99] disabled:opacity-60"
          :disabled="busy || !agreed"
          @click="startGoogleLogin"
        >
          <svg class="h-6 w-6 shrink-0" viewBox="0 0 48 48" aria-hidden="true">
            <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
            <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
            <path fill="#FBBC05" d="M10.53 28.59A14.3 14.3 0 0 1 9.77 24c0-1.6.27-3.14.76-4.59l-7.98-6.19A24 24 0 0 0 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
            <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
          </svg>
          <span>{{ busy ? '正在跳转…' : '使用 Google 账号继续' }}</span>
        </button>

        <label class="mt-6 flex cursor-pointer items-start gap-2 text-xs leading-relaxed text-outline">
          <input v-model="agreed" type="checkbox" class="mt-0.5 rounded border-outline-variant text-primary focus:ring-primary" />
          <span>
            我已阅读并同意
            <RouterLink class="font-medium text-primary hover:underline" to="/terms">《用户服务协议》</RouterLink>
            与
            <RouterLink class="font-medium text-primary hover:underline" to="/privacy">《隐私保护政策》</RouterLink>
          </span>
        </label>
      </div>

      <p class="mt-6 text-center text-xs leading-relaxed text-outline">
        登录凭据由 Google 安全处理，视书不会获取或保存你的 Google 密码。
      </p>
    </section>
  </main>
</template>
