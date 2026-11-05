<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { clearLlmSettings, getLlmSettings, saveLlmSettings, setLlmMode } from '../../api'
import type { LlmMode, LlmSettings } from '../../api/live/llm-settings'

const baseUrl = ref('')
const apiKey = ref('')
const model = ref('')
const hasKey = ref(false)
const configured = ref(false)
/** 服务端当前生效的模式 */
const activeMode = ref<LlmMode>('auto')
/** 页面上选中的分段（未保存自定义前选「自定义」只展开表单，不改变生效模式） */
const selectedMode = ref<LlmMode>('auto')
const systemModel = ref('')
const showKey = ref(false)
const loading = ref(true)
const busy = ref(false)
const loaded = ref(false)
const error = ref('')
const message = ref('')

const statusText = computed(() => {
  if (loading.value) return '加载中…'
  if (!loaded.value) return '未能加载'
  return activeMode.value === 'custom' ? '当前使用：自定义接口' : '当前使用：Auto（项目提供）'
})

function apply(settings: LlmSettings) {
  baseUrl.value = settings.base_url
  model.value = settings.model
  hasKey.value = settings.has_api_key
  configured.value = settings.configured
  activeMode.value = settings.mode
  selectedMode.value = settings.mode
  systemModel.value = settings.system_model
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

async function choose(mode: LlmMode) {
  if (busy.value || !loaded.value || mode === selectedMode.value) return
  error.value = ''
  message.value = ''
  // 尚未保存完整的自定义接口：只展开表单，保存后才切换生效。
  if (mode === 'custom' && !configured.value) {
    selectedMode.value = 'custom'
    return
  }
  if (mode === activeMode.value) {
    selectedMode.value = mode
    return
  }
  busy.value = true
  try {
    apply(await setLlmMode(mode))
    message.value = mode === 'auto'
      ? '已切换到 Auto，后续任务使用项目提供的模型；你的自定义配置仍保留。'
      : '已切换到自定义接口，后续启动的转换任务将使用你的配置。'
  } catch {
    error.value = '切换失败，请稍后重试。'
  } finally {
    busy.value = false
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
    message.value = '配置已保存并启用，后续启动的转换任务将使用此配置。'
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
    message.value = '自定义配置已删除，后续任务使用 Auto（项目提供）。'
  } catch {
    error.value = '删除失败，请稍后重试。'
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
        <span class="font-title-sm text-title-sm text-on-surface">AI 模型来源</span>
        <span class="text-label-sm" :class="activeMode === 'custom' ? 'text-primary' : 'text-outline'">{{ statusText }}</span>
      </div>
      <div role="radiogroup" aria-label="AI 模型来源" class="grid grid-cols-2 gap-space-xs rounded-lg bg-surface-container p-1">
        <button id="llm-mode-auto" type="button" role="radio" :aria-checked="selectedMode === 'auto'" :disabled="loading || busy || !loaded"
          class="flex items-center justify-center gap-space-xs rounded-md py-2 px-3 text-label-lg disabled:opacity-60"
          :class="selectedMode === 'auto' ? 'bg-surface-container-lowest text-primary shadow-sm' : 'text-on-surface-variant'"
          @click="choose('auto')">
          <span class="material-symbols-outlined text-[18px]" aria-hidden="true">auto_awesome</span>
          Auto
        </button>
        <button id="llm-mode-custom" type="button" role="radio" :aria-checked="selectedMode === 'custom'" :disabled="loading || busy || !loaded"
          class="flex items-center justify-center gap-space-xs rounded-md py-2 px-3 text-label-lg disabled:opacity-60"
          :class="selectedMode === 'custom' ? 'bg-surface-container-lowest text-primary shadow-sm' : 'text-on-surface-variant'"
          @click="choose('custom')">
          <span class="material-symbols-outlined text-[18px]" aria-hidden="true">tune</span>
          自定义
        </button>
      </div>
      <div v-show="selectedMode === 'auto'" class="flex flex-col gap-space-xs rounded-lg border border-outline-variant p-space-sm">
        <div class="flex items-center gap-space-xs">
          <span class="text-label-md text-on-surface">项目提供</span>
          <span v-if="systemModel" class="rounded-full bg-surface-container px-2 py-0.5 text-label-sm text-on-surface-variant">{{ systemModel }}</span>
        </div>
        <p class="text-body-sm text-on-surface-variant">无需配置，使用本服务提供的模型完成转换。</p>
        <p v-if="configured" class="text-body-sm text-outline">你保存的自定义接口仍保留，切换到「自定义」即可启用。</p>
      </div>
      <p v-show="selectedMode === 'custom'" class="text-body-sm text-on-surface-variant">填写你自己的 OpenAI 兼容接口，仅用于你的转换任务。保存后立即启用；保存配置不会验证上游余额或可用性。</p>
      <fieldset v-show="selectedMode === 'custom'" :disabled="loading || busy || !loaded" class="flex flex-col gap-space-md disabled:opacity-60 min-w-0">
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
          <input id="llm-model" v-model="model" type="text" maxlength="200" autocomplete="off" :placeholder="systemModel ? `留空使用 ${systemModel}` : '留空使用项目默认模型'"
            class="w-full rounded-lg border border-outline-variant bg-surface px-3 py-3 text-body-md text-on-surface focus:outline-none focus:border-primary" />
        </div>
        <div class="flex flex-wrap gap-space-sm">
          <button type="submit" class="flex-1 rounded-lg bg-primary text-on-primary py-3 px-4 text-label-lg">{{ busy ? '处理中…' : '保存并启用' }}</button>
          <button type="button" :disabled="!hasKey" class="rounded-lg border border-outline-variant text-on-surface-variant py-3 px-4 text-label-md disabled:opacity-40" @click="clear">删除自定义配置</button>
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
