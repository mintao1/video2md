<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { clearLlmSettings, getLlmSettings, saveLlmSettings } from '../../api'
import type { LlmSettings } from '../../api/live/llm-settings'

const baseUrl = ref('')
const apiKey = ref('')
const model = ref('')
const hasKey = ref(false)
const configured = ref(false)
const showKey = ref(false)
const loading = ref(true)
const busy = ref(false)
const loaded = ref(false)
const error = ref('')
const message = ref('')

function apply(settings: LlmSettings) {
  baseUrl.value = settings.base_url
  model.value = settings.model
  hasKey.value = settings.has_api_key
  configured.value = settings.configured
  apiKey.value = ''
  showKey.value = false
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    apply(await getLlmSettings())
    loaded.value = true
  } catch {
    error.value = '无法加载 LLM 配置，请重试。'
  } finally {
    loading.value = false
  }
}

async function save() {
  if (busy.value || !loaded.value) return
  error.value = ''
  message.value = ''
  if (!hasKey.value && !apiKey.value.trim()) {
    error.value = '首次配置请填写 API Key。'
    return
  }
  busy.value = true
  try {
    apply(await saveLlmSettings({
      base_url: baseUrl.value.trim(),
      ...(apiKey.value.trim() ? { api_key: apiKey.value.trim() } : {}),
      model: model.value.trim(),
    }))
    message.value = '配置已保存，后续启动的转换任务将使用此配置。'
  } catch {
    error.value = '保存失败，请检查 API URL、Key 和模型格式，或稍后重试。'
  } finally {
    busy.value = false
  }
}

async function clear() {
  if (busy.value || !loaded.value) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    apply(await clearLlmSettings())
    message.value = '个人 LLM 配置已清除，后续任务使用服务默认配置。'
  } catch {
    error.value = '清除失败，请稍后重试。'
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <section aria-labelledby="llm-settings-title" class="flex flex-col gap-space-sm">
    <h2 id="llm-settings-title" class="font-title-sm text-title-sm text-on-surface px-space-xs">LLM API 配置</h2>
    <form class="bg-surface-container-lowest rounded-xl shadow-sm p-space-md flex flex-col gap-space-md" @submit.prevent="save">
      <div class="flex items-center justify-between gap-space-sm">
        <span class="font-title-sm text-title-sm text-on-surface">自定义 AI 服务</span>
        <span class="text-label-sm" :class="configured ? 'text-primary' : 'text-outline'">
          {{ loading ? '加载中…' : !loaded ? '未能加载' : configured ? '已保存个人配置' : '使用服务默认配置' }}
        </span>
      </div>
      <p class="text-body-sm text-on-surface-variant">填写 OpenAI 兼容接口，仅用于你的转换任务。保存配置不会验证上游余额或可用性。</p>
      <fieldset :disabled="loading || busy || !loaded" class="flex flex-col gap-space-md disabled:opacity-60 min-w-0">
        <div class="flex flex-col gap-space-xs">
          <label for="llm-api-url" class="text-label-md text-on-surface">API URL</label>
          <input id="llm-api-url" v-model="baseUrl" type="url" required maxlength="2048" autocomplete="off" placeholder="https://api.example.com/v1"
            class="w-full rounded-lg border border-outline-variant bg-surface px-3 py-3 text-body-md text-on-surface focus:outline-none focus:border-primary" />
          <p class="text-body-sm text-outline">填写 Base URL（通常以 /v1 结尾），后端会追加 /chat/completions。</p>
        </div>
        <div class="flex flex-col gap-space-xs">
          <label for="llm-api-key" class="text-label-md text-on-surface">API Key</label>
          <div class="flex gap-space-xs">
            <input id="llm-api-key" v-model="apiKey" :type="showKey ? 'text' : 'password'" :required="!hasKey" maxlength="4096" autocomplete="new-password" :spellcheck="false"
              :placeholder="hasKey ? '已保存，留空保留原 Key' : '请输入 API Key'"
              class="min-w-0 flex-1 rounded-lg border border-outline-variant bg-surface px-3 py-3 text-body-md text-on-surface focus:outline-none focus:border-primary" />
            <button type="button" :aria-label="showKey ? '隐藏 API Key' : '显示 API Key'" :aria-pressed="showKey" class="px-3 rounded-lg text-primary bg-surface-container" @click="showKey = !showKey">
              {{ showKey ? '隐藏' : '显示' }}
            </button>
          </div>
          <p class="text-body-sm text-outline">Key 保存在服务端，读取配置时不回传，不存入浏览器本地存储。</p>
        </div>
        <div class="flex flex-col gap-space-xs">
          <label for="llm-model" class="text-label-md text-on-surface">模型名称（可选）</label>
          <input id="llm-model" v-model="model" type="text" maxlength="200" autocomplete="off" placeholder="留空使用服务默认模型"
            class="w-full rounded-lg border border-outline-variant bg-surface px-3 py-3 text-body-md text-on-surface focus:outline-none focus:border-primary" />
        </div>
        <div class="flex flex-wrap gap-space-sm">
          <button type="submit" class="flex-1 rounded-lg bg-primary text-on-primary py-3 px-4 text-label-lg">{{ busy ? '处理中…' : '保存 LLM 配置' }}</button>
          <button type="button" :disabled="!hasKey" class="rounded-lg border border-outline-variant text-on-surface-variant py-3 px-4 text-label-md disabled:opacity-40" @click="clear">恢复服务默认</button>
        </div>
      </fieldset>
      <p v-if="error" role="alert" class="text-body-sm text-error">{{ error }}</p>
      <button v-if="!loaded && !loading" type="button" class="text-primary text-label-md self-start" @click="load">重新加载配置</button>
      <p v-if="message" role="status" class="text-body-sm text-primary">{{ message }}</p>
    </form>
    <nav aria-label="算力平台登录" class="bg-surface-container-lowest rounded-xl shadow-sm p-space-md flex flex-col gap-space-sm">
      <p class="text-body-sm text-on-surface-variant">连接你自己的 Colab；Kaggle 登录将在新标签页打开。</p>
      <div class="grid grid-cols-1 min-[360px]:grid-cols-2 gap-space-sm">
        <RouterLink to="/profile/colab"
          class="flex items-center justify-center gap-space-xs rounded-lg bg-primary text-on-primary py-3 px-4 text-label-lg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary">
          登录 Colab
          <span class="material-symbols-outlined text-[18px]" aria-hidden="true">cloud_sync</span>
        </RouterLink>
        <a href="https://www.kaggle.com/account/login" target="_blank" rel="noopener noreferrer"
          class="flex items-center justify-center gap-space-xs rounded-lg border border-outline-variant text-primary py-3 px-4 text-label-lg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary">
          登录 Kaggle
          <span class="material-symbols-outlined text-[18px]" aria-hidden="true">open_in_new</span>
        </a>
      </div>
    </nav>
  </section>
</template>
