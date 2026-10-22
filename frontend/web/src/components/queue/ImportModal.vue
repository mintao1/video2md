<script setup lang="ts">
import { ref, watch } from 'vue'
import BottomSheet from '../common/BottomSheet.vue'

const props = defineProps<{ modelValue: boolean; error?: string; busy?: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'start', payload: { url: string; title?: string }): void
  (e: 'draft'): void
}>()

const source = ref<'link' | 'cloud' | 'local'>('link')
const url = ref('')
const ocrEnabled = ref(true)
const tocEnabled = ref(true)
const template = ref<'academic' | 'notes'>('academic')
const validationError = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      validationError.value = ''
      if (!url.value) url.value = 'https://www.bilibili.com/video/BV1WE411d7v4'
    }
  },
)

function start() {
  if (props.busy) return
  validationError.value = ''
  if (source.value === 'link' && !url.value.trim()) {
    validationError.value = '请输入视频或音频链接'
    return
  }
  emit('start', {
    url: url.value.trim() || '本地上传视频.mp4',
    title:
      source.value === 'local'
        ? '本地上传视频'
        : source.value === 'cloud'
          ? '网盘提取视频'
          : undefined,
  })
}

function saveDraft() {
  emit('draft')
  emit('update:modelValue', false)
}
</script>

<template>
  <BottomSheet
    :model-value="modelValue"
    icon="video_settings"
    title="添加新的网课转换"
    subtitle="支持解析高清字幕与原画"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="flex flex-col gap-space-sm pb-2">
      <!-- 来源 tab -->
      <div class="grid grid-cols-3 gap-space-xs p-0.5 bg-surface-container rounded-lg">
        <button
          v-for="s in [
            { key: 'link', label: '链接导入', icon: 'link' },
            { key: 'cloud', label: '网盘提取', icon: 'cloud_download' },
            { key: 'local', label: '本地 MP4', icon: 'upload_file' },
          ]"
          :key="s.key"
          :disabled="busy"
          class="py-1.5 rounded-md font-label-md text-label-md flex items-center justify-center gap-1 transition-all"
          :class="
            source === s.key
              ? 'bg-surface-container-lowest shadow-sm text-primary font-semibold'
              : 'text-on-surface-variant active:scale-95'
          "
          @click="source = s.key as typeof source"
        >
          <span class="material-symbols-outlined text-[16px]">{{ s.icon }}</span>
          {{ s.label }}
        </button>
      </div>

      <!-- 链接输入 / 本地上传说明 -->
      <div
        v-if="source === 'link'"
        class="bg-surface-container-low rounded-xl p-space-sm border border-outline-variant/30 flex items-center gap-space-sm shadow-sm"
      >
        <span class="material-symbols-outlined text-outline text-[20px]">link</span>
        <input
          v-model="url"
          :disabled="busy"
          class="bg-transparent flex-1 text-on-surface font-body-md text-body-md focus:outline-none placeholder:text-outline min-w-0"
          placeholder="粘贴 Bilibili / YouTube / 夸克 / 百度网盘链接..."
          type="text"
        />
        <button
          :disabled="busy"
          class="bg-primary text-on-primary font-label-md text-label-md px-3 py-1.5 rounded-lg shrink-0 shadow-sm active:scale-95 transition-transform"
          @click="url = ''"
        >
          清空
        </button>
      </div>
      <p v-if="validationError || error" class="text-xs text-red-600 px-1" role="alert">{{ validationError || error }}</p>
      <div
        v-else
        class="bg-surface-container-low rounded-xl p-space-md border border-outline-variant/30 flex flex-col items-center gap-space-xs shadow-sm"
      >
        <span class="material-symbols-outlined text-outline text-[32px]">
          {{ source === 'local' ? 'upload_file' : 'cloud_download' }}
        </span>
        <span class="font-body-sm text-body-sm text-on-surface-variant">
          {{ source === 'local' ? '点击选择本地 MP4 文件（mock：直接开始）' : '粘贴夸克/百度网盘分享链接' }}
        </span>
      </div>
      <p v-if="busy" role="status" aria-live="polite" class="text-sm text-on-surface-variant">
        正在读取视频与合集信息，请稍候。解析成功后会自动加入队列。
      </p>
      <div class="flex items-center gap-space-xs text-[11px] text-outline px-1">
        <span class="material-symbols-outlined text-[14px]">info</span>
        <span>已自动读取剪贴板链接 · 支持分P全集一键解析</span>
      </div>

      <!-- 高级配置 -->
      <div class="flex flex-col gap-space-sm pt-space-xs border-t border-outline-variant/30">
        <span class="font-label-md text-label-md text-on-surface-variant">高级智能识别配置</span>
        <div class="flex flex-col gap-space-xs bg-surface-container-low rounded-xl p-space-sm">
          <div class="flex items-center justify-between py-1">
            <div class="flex flex-col">
              <span class="font-title-sm text-[13px] text-on-surface font-semibold flex items-center gap-1">
                <span class="material-symbols-outlined text-[16px] text-primary">functions</span>
                智能板书与公式提取 (OCR / LaTeX)
              </span>
              <span class="text-[11px] text-outline">重构黑板草稿与 PPT 为清晰印刷体排版</span>
            </div>
            <button
              role="switch"
              :aria-checked="ocrEnabled"
              class="w-10 h-6 rounded-full relative shrink-0 transition-colors p-0.5"
              :class="ocrEnabled ? 'bg-secondary-container' : 'bg-surface-container-highest'"
              @click="ocrEnabled = !ocrEnabled"
            >
              <span
                class="block w-4 h-4 bg-on-primary rounded-full shadow-sm transition-transform"
                :class="ocrEnabled ? 'ml-auto' : 'translate-x-0'"
              />
            </button>
          </div>
          <div class="h-[1px] bg-outline-variant/30" />
          <div class="flex items-center justify-between py-1">
            <div class="flex flex-col">
              <span class="font-title-sm text-[13px] text-on-surface font-semibold flex items-center gap-1">
                <span class="material-symbols-outlined text-[16px] text-secondary">account_tree</span>
                自动生成章节目录与定理证明
              </span>
              <span class="text-[11px] text-outline">基于知识点树状归纳，提取重要推导结论</span>
            </div>
            <button
              role="switch"
              :aria-checked="tocEnabled"
              class="w-10 h-6 rounded-full relative shrink-0 transition-colors p-0.5"
              :class="tocEnabled ? 'bg-secondary-container' : 'bg-surface-container-highest'"
              @click="tocEnabled = !tocEnabled"
            >
              <span
                class="block w-4 h-4 bg-on-primary rounded-full shadow-sm transition-transform"
                :class="tocEnabled ? 'ml-auto' : 'translate-x-0'"
              />
            </button>
          </div>
        </div>
      </div>

      <!-- 模板选择 -->
      <div class="flex flex-col gap-space-xs">
        <label class="font-label-md text-label-md text-on-surface-variant">教材排版模板</label>
        <div class="grid grid-cols-2 gap-space-sm">
          <button
            class="p-space-sm rounded-xl border-2 flex flex-col gap-0.5 text-left transition-all"
            :class="
              template === 'academic'
                ? 'border-primary bg-primary/10'
                : 'border-outline-variant/40 bg-surface-container-lowest opacity-80 hover:opacity-100'
            "
            @click="template = 'academic'"
          >
            <div class="flex items-center justify-between w-full">
              <span
                class="font-title-sm text-[13px] font-semibold flex items-center gap-1"
                :class="template === 'academic' ? 'text-primary' : 'text-on-surface'"
              >
                <span class="material-symbols-outlined text-[16px]">menu_book</span>
                学术专著风格
              </span>
              <span v-if="template === 'academic'" class="material-symbols-outlined text-primary text-[16px]">
                check_circle
              </span>
            </div>
            <span class="text-[10px] text-on-surface-variant">典雅双栏 · 附定理定义标注块</span>
          </button>
          <button
            class="p-space-sm rounded-xl border-2 flex flex-col gap-0.5 text-left transition-all"
            :class="
              template === 'notes'
                ? 'border-primary bg-primary/10'
                : 'border-outline-variant/40 bg-surface-container-lowest opacity-80 hover:opacity-100'
            "
            @click="template = 'notes'"
          >
            <div class="flex items-center justify-between w-full">
              <span
                class="font-title-sm text-[13px] font-semibold flex items-center gap-1"
                :class="template === 'notes' ? 'text-primary' : 'text-on-surface'"
              >
                <span class="material-symbols-outlined text-[16px]">edit_note</span>
                考研精讲手写体
              </span>
              <span v-if="template === 'notes'" class="material-symbols-outlined text-primary text-[16px]">
                check_circle
              </span>
            </div>
            <span class="text-[10px] text-outline">重点荧光高亮 · 边注心得便签</span>
          </button>
        </div>
      </div>

      <!-- 动作 -->
      <div class="flex items-center gap-space-sm pt-space-xs pb-1">
        <button
          :disabled="busy"
          class="flex-1 py-3 rounded-xl bg-surface-container text-on-surface font-title-sm text-[14px] font-semibold active:scale-[0.98] transition-transform"
          @click="saveDraft"
        >
          保存草稿
        </button>
        <button
          :disabled="busy"
          :aria-busy="busy"
          class="flex-[2] py-3 rounded-xl bg-primary text-on-primary font-title-sm text-[14px] font-semibold shadow-md active:scale-[0.98] transition-transform flex items-center justify-center gap-1.5"
          @click="start"
        >
          <span class="material-symbols-outlined text-[18px]">rocket_launch</span>
          {{ busy ? '正在解析…' : '立即开始转换排队' }}
        </button>
      </div>
    </div>
  </BottomSheet>
</template>
