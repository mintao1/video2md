<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { BASE_URL, safeRedirect } from '../api/client'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const error = ref('')
const busy = ref(false)
const redirect = computed(() => safeRedirect(route.query.redirect))
const notice = computed(() => (route.query.reason === 'expired' ? '登录已过期，请重新登录' : ''))

onMounted(() => {
  const fragment = new URLSearchParams(window.location.hash.slice(1))
  const token = fragment.get('id_token')
  const authError = fragment.get('auth_error')
  window.history.replaceState({}, document.title, `${window.location.pathname}${window.location.search}`)
  if (token) {
    auth.acceptToken(token)
    router.replace(redirect.value)
  } else if (authError) {
    error.value = authError
  }
})

function startGoogleLogin(): void {
  busy.value = true
  // Local Vite runs on a different origin than the API; production shares one.
  // The backend keeps the target with the OAuth state and returns it as ?redirect=.
  const query = redirect.value === '/library' ? '' : `?${new URLSearchParams({ redirect: redirect.value })}`
  window.location.assign(`${BASE_URL}/auth/google/login${query}`)
}
</script>

<template>
  <main class="min-h-screen grid place-items-center bg-surface p-6">
    <section class="w-full max-w-md rounded-2xl border border-outline bg-white p-8 shadow-sm">
      <h1 class="text-2xl font-semibold">登录 video2md</h1>
      <p class="mt-2 text-body-sm text-outline">使用 Google 账号继续</p>
      <p v-if="notice && !error" class="mt-5 rounded-lg bg-surface-container p-3 text-body-sm text-on-surface">{{ notice }}</p>
      <p v-if="error" class="mt-5 rounded-lg bg-red-50 p-3 text-body-sm text-red-700">{{ error }}</p>
      <button class="mt-8 w-full rounded-lg bg-primary px-4 py-3 font-medium text-white disabled:opacity-60" :disabled="busy" @click="startGoogleLogin">
        {{ busy ? '正在跳转…' : '使用 Google 登录' }}
      </button>
      <p class="mt-5 text-xs text-outline">登录后才能访问教材库、任务队列和个人资料。</p>
      <p class="mt-3 text-xs text-outline">
        继续即表示你已阅读并同意
        <RouterLink class="text-primary underline" to="/terms">《用户服务协议》</RouterLink>
        与
        <RouterLink class="text-primary underline" to="/privacy">《隐私保护政策》</RouterLink>。
      </p>
    </section>
  </main>
</template>
