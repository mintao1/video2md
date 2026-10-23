<script setup lang="ts">
import { ref, watch } from 'vue'
import BottomSheet from '../common/BottomSheet.vue'

const props = defineProps<{ modelValue: boolean; error?: string; busy?: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'start', payload: { url: string; title?: string }): void
}>()

const url = ref('')
const validationError = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (open) validationError.value = ''
  },
)

function start() {
  if (props.busy) return
  validationError.value = ''
  if (!url.value.trim()) {
    validationError.value = '请输入视频链接'
    return
  }
  emit('start', { url: url.value.trim() })
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
      <!-- 链接输入 -->
      <div
        class="bg-surface-container-low rounded-xl p-space-sm border border-outline-variant/30 flex items-center gap-space-sm shadow-sm"
      >
        <span class="material-symbols-outlined text-outline text-[20px]">link</span>
        <input
          v-model="url"
          :disabled="busy"
          class="bg-transparent flex-1 text-on-surface font-body-md text-body-md focus:outline-none placeholder:text-outline min-w-0"
          placeholder="粘贴 Bilibili / YouTube / 抖音 / 小红书链接..."
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
      <p v-if="validationError || error" class="text-xs text-red-600 px-1" role="alert">
        {{ validationError || error }}
      </p>
      <p v-if="busy" role="status" aria-live="polite" class="text-sm text-on-surface-variant">
        正在读取视频与合集信息，请稍候。解析成功后会自动加入队列。
      </p>
      <p class="text-[11px] text-outline px-1">支持 Bilibili、YouTube、抖音、小红书链接；合集会自动展开为多讲。</p>

      <!-- 动作 -->
      <div class="flex items-center gap-space-sm pt-space-xs pb-1">
        <button
          :disabled="busy"
          :aria-busy="busy"
          class="flex-1 py-3 rounded-xl bg-primary text-on-primary font-title-sm text-[14px] font-semibold shadow-md active:scale-[0.98] transition-transform flex items-center justify-center gap-1.5"
          @click="start"
        >
          <span class="material-symbols-outlined text-[18px]">rocket_launch</span>
          {{ busy ? '正在解析…' : '立即开始转换排队' }}
        </button>
      </div>
    </div>
  </BottomSheet>
</template>
