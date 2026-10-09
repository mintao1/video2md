<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '../api'
import { BASE_URL } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useToastStore } from '../stores/toast'
import { useCountdown } from '../composables/useCountdown'
import LogoMark from '../components/art/LogoMark.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const toast = useToastStore()

const mode = ref<'code' | 'pwd'>('code')
const phone = ref('')
const code = ref('')
const password = ref('')
const showPassword = ref(false)
const agree = ref(false)
const submitting = ref(false)
const showClear = ref(false)
const { remaining, running, start } = useCountdown(60)

function switchMode(m: 'code' | 'pwd') {
  mode.value = m
}

function onPhoneInput() {
  showClear.value = phone.value.length > 0
}

function clearPhone() {
  phone.value = ''
  showClear.value = false
}

async function sendCode() {
  if (running.value) return
  if (phone.value.length !== 11) {
    toast.show('请输入完整的11位手机号码')
    return
  }
  await api.sendSmsCode(phone.value)
  start()
  toast.show('验证码已发送（mock：随便填 6 位即可）')
}

function requireAgreement(): boolean {
  if (!agree.value) {
    toast.show('请勾选并同意《用户服务协议》与《隐私保护政策》')
    return false
  }
  return true
}

function requirePhone(): boolean {
  if (phone.value.length !== 11) {
    toast.show('请输入有效手机号')
    return false
  }
  return true
}

async function submit() {
  if (submitting.value || !requireAgreement() || !requirePhone()) return
  submitting.value = true
  try {
    const result =
      mode.value === 'code'
        ? await api.loginWithSms(phone.value, code.value)
        : await api.loginWithPassword(phone.value, password.value)
    await auth.login(result)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/library'
    router.replace(redirect)
  } finally {
    submitting.value = false
  }
}

async function wechatLogin() {
  if (!requireAgreement()) return
  submitting.value = true
  try {
    const result = await api.loginWithWechat()
    await auth.login(result)
    router.replace('/library')
  } finally {
    submitting.value = false
  }
}

const POST_LOGIN_REDIRECT_KEY = 'v2md_post_login_redirect'

function googleLogin() {
  if (!requireAgreement()) return
  // 授权码流是整页跳转，?redirect= 会丢——先存 sessionStorage，回跳后消费
  if (typeof route.query.redirect === 'string' && route.query.redirect) {
    sessionStorage.setItem(POST_LOGIN_REDIRECT_KEY, route.query.redirect)
  }
  window.location.href = `${BASE_URL}/auth/google/login`
}

// Google 授权码流回跳：后端 callback 302 回 /login#id_token=…（或 #auth_error=…），
// 整页加载非 SPA 内跳转，setup 期挂载时同步消费 fragment 后立即从 URL 清掉。
onMounted(async () => {
  const params = new URLSearchParams(window.location.hash.slice(1))
  const idToken = params.get('id_token')
  const authError = params.get('auth_error')
  if (!idToken && !authError) return
  history.replaceState(null, '', window.location.pathname + window.location.search)
  if (authError) {
    toast.show(authError)
    return
  }
  try {
    await auth.loginWithIdToken(idToken!)
    const redirect = sessionStorage.getItem(POST_LOGIN_REDIRECT_KEY) ?? '/library'
    sessionStorage.removeItem(POST_LOGIN_REDIRECT_KEY)
    router.replace(redirect)
  } catch {
    auth.logout() // token 已写入但 /me 失败 = 凭据不可用，回滚留干净登录页
    toast.show('Google 登录失败：无法获取账号信息，请重试')
  }
})
</script>

<template>
  <main class="flex flex-col relative w-full pt-safe pb-safe bg-surface min-h-screen">
    <div class="flex flex-col w-full pb-8">
      <!-- Logo & 标题 -->
      <div class="px-4 pt-8 pb-2 flex flex-col items-center text-center">
        <LogoMark class="w-20 h-20 !rounded-2xl mb-3 shadow-md" />
        <h1 class="font-headline-lg-mobile text-headline-lg-mobile text-on-surface tracking-tight">
          视书
        </h1>
        <p class="font-body-sm text-body-sm text-outline mt-1">视频一键转成可精读的教材</p>
      </div>

      <!-- 登录卡片 -->
      <div class="px-4 mt-6">
        <div class="bg-surface-container-lowest rounded-xl p-4 shadow-md">
          <!-- Tab 切换 -->
          <div class="flex items-center justify-between pb-3 mb-3">
            <div class="flex items-center gap-4">
              <button
                class="font-title-sm pb-1 relative transition-colors"
                :class="mode === 'code' ? 'text-primary font-bold' : 'text-outline font-semibold'"
                @click="switchMode('code')"
              >
                快捷验证码
                <span
                  v-if="mode === 'code'"
                  class="absolute bottom-0 left-0 w-full h-[2.5px] bg-primary rounded-full"
                />
              </button>
              <button
                class="font-title-sm pb-1 relative transition-colors"
                :class="mode === 'pwd' ? 'text-primary font-bold' : 'text-outline font-semibold'"
                @click="switchMode('pwd')"
              >
                密码登录
                <span
                  v-if="mode === 'pwd'"
                  class="absolute bottom-0 left-0 w-full h-[2.5px] bg-primary rounded-full"
                />
              </button>
            </div>
          </div>

          <form class="flex flex-col gap-3" @submit.prevent="submit">
            <!-- 手机号 -->
            <div class="bg-surface-container-low rounded-lg p-2.5 flex items-center gap-2">
              <div class="flex items-center gap-0.5 pr-2">
                <span class="font-label-md text-label-md text-on-surface font-medium">+86</span>
                <span class="material-symbols-outlined text-[16px] text-outline">arrow_drop_down</span>
              </div>
              <div class="h-4 w-[1px] bg-outline-variant" />
              <input
                v-model="phone"
                class="bg-transparent font-body-md text-body-md text-on-surface placeholder:text-outline w-full focus:outline-none"
                maxlength="11"
                placeholder="请输入手机号码"
                type="tel"
                inputmode="numeric"
                @input="onPhoneInput"
              />
              <button
                v-if="showClear"
                type="button"
                class="text-outline hover:text-on-surface p-1"
                @click="clearPhone"
              >
                <span class="material-symbols-outlined text-[16px]">cancel</span>
              </button>
            </div>

            <!-- 验证码 -->
            <div v-if="mode === 'code'" class="bg-surface-container-low rounded-lg p-2.5 flex items-center gap-2">
              <span class="material-symbols-outlined text-[20px] text-outline">shield</span>
              <input
                v-model="code"
                class="bg-transparent font-body-md text-body-md text-on-surface placeholder:text-outline w-full focus:outline-none"
                maxlength="6"
                placeholder="请输入6位验证码"
                type="tel"
                inputmode="numeric"
              />
              <button
                type="button"
                class="px-2.5 py-1.5 rounded-lg bg-surface-container-highest text-primary font-label-sm font-semibold shrink-0 transition-all active:opacity-80"
                :class="running ? 'opacity-50' : ''"
                :disabled="running"
                @click="sendCode"
              >
                {{ running ? `${remaining}s 后重发` : '获取验证码' }}
              </button>
            </div>

            <!-- 密码 -->
            <div v-else class="bg-surface-container-low rounded-lg p-2.5 flex items-center gap-2">
              <span class="material-symbols-outlined text-[20px] text-outline">lock</span>
              <input
                v-model="password"
                class="bg-transparent font-body-md text-body-md text-on-surface placeholder:text-outline w-full focus:outline-none"
                placeholder="请输入登录密码"
                :type="showPassword ? 'text' : 'password'"
              />
              <button
                type="button"
                class="text-outline p-1 hover:text-on-surface"
                @click="showPassword = !showPassword"
              >
                <span class="material-symbols-outlined text-[18px]">
                  {{ showPassword ? 'visibility' : 'visibility_off' }}
                </span>
              </button>
            </div>

            <button
              type="button"
              class="w-full mt-2 py-3 rounded-xl bg-gradient-to-r from-primary to-primary-container text-on-primary font-title-sm shadow-md shadow-primary/20 flex items-center justify-center gap-1.5 active:scale-[0.99] transition-transform"
              :class="submitting ? 'opacity-80' : ''"
              :disabled="submitting"
              @click="submit"
            >
              <span
                v-if="submitting"
                class="material-symbols-outlined text-[18px] animate-spin"
              >
                progress_activity
              </span>
              <span>{{ submitting ? '正在验证登录...' : '登录 / 注册新账号' }}</span>
              <span v-if="!submitting" class="material-symbols-outlined text-[18px]">arrow_forward</span>
            </button>
          </form>

          <!-- 其他方式 -->
          <div class="relative flex items-center justify-center my-4">
            <div class="w-full h-[1px] bg-surface-container-high" />
            <span class="absolute bg-surface-container-lowest px-3 font-label-sm text-label-sm text-outline">
              或使用其他快捷方式
            </span>
          </div>
          <div class="flex flex-col gap-3 w-full">
            <button
              class="flex items-center justify-center gap-2 py-2.5 px-3 rounded-lg bg-surface-container-low hover:bg-surface active:scale-[0.98] transition-all w-full"
              @click="wechatLogin"
            >
              <div class="w-6 h-6 rounded-full bg-emerald-500 flex items-center justify-center text-white">
                <span class="material-symbols-outlined text-[15px]">chat</span>
              </div>
              <span class="font-body-sm text-body-sm font-semibold text-on-surface">微信一键登录</span>
            </button>
            <button
              class="flex items-center justify-center gap-2 py-2.5 px-3 rounded-lg bg-surface-container-low hover:bg-surface active:scale-[0.98] transition-all w-full"
              @click="googleLogin"
            >
              <svg class="w-6 h-6 shrink-0" viewBox="0 0 48 48" aria-hidden="true">
                <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
                <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
                <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>
                <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
              </svg>
              <span class="font-body-sm text-body-sm font-semibold text-on-surface">Google 登录</span>
            </button>
          </div>
        </div>
      </div>

      <!-- 协议 -->
      <div class="px-6 mt-6 flex items-center justify-center text-center">
        <label class="flex items-start justify-center gap-2 cursor-pointer select-none">
          <input
            v-model="agree"
            type="checkbox"
            class="mt-0.5 rounded border-outline-variant text-primary focus:ring-0"
          />
          <span class="font-body-sm text-[11px] text-outline leading-relaxed">
            已阅读并同意
            <a class="text-primary hover:underline" href="javascript:void(0)">《用户服务协议》</a>
            与
            <a class="text-primary hover:underline" href="javascript:void(0)">《隐私保护政策》</a>
            ，未注册手机号将自动完成注册
          </span>
        </label>
      </div>
    </div>
  </main>
</template>
