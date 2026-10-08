<script setup lang="ts">
import { computed, ref } from 'vue'
import BottomSheet from '../common/BottomSheet.vue'

defineProps<{ modelValue: boolean }>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'pay', cycle: string): void
}>()

const cycle = ref<'monthly' | 'quarterly' | 'yearly'>('yearly')

const svipFeatures = [
  '<strong>无限量</strong> 视频转书时长（无单月用量上限）',
  '<strong>独立 A100 GPU 算力通道</strong> · 免排队秒级调度',
  '<strong>4K 超清板书提纯</strong> · 智能去除手影/反光/偏色',
  '<strong>全学科专著排版模板库</strong> + LaTeX 源码实时同步',
  '<strong>一键双向同步 Notion &amp; Obsidian</strong> 知识图谱',
]

const proFeatures = ['每月 50 小时 4K 转录', 'Whisper-v3 语音转写', '20GB 专属云端库空间', '导出 LaTeX & 高清 PDF']

const price = computed(() => {
  switch (cycle.value) {
    case 'yearly':
      return { amount: '588', note: '(立省 ¥228)', sub: '年付 ¥588 / 年' }
    case 'quarterly':
      return { amount: '207', note: '(立省 ¥33)', sub: '季付 ¥207 / 季' }
    default:
      return { amount: '88', note: '', sub: '连续包月 ¥88 / 月' }
  }
})
</script>

<template>
  <BottomSheet :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)">
    <template #header>
      <div class="flex items-center gap-2">
        <div
          class="w-8 h-8 rounded-lg bg-gradient-to-tr from-primary to-secondary flex items-center justify-center text-on-primary shadow-sm"
        >
          <span class="material-symbols-outlined text-[18px] filled">workspace_premium</span>
        </div>
        <div>
          <h2 class="font-headline-md text-headline-md text-on-surface leading-tight">选择会员等级与特权</h2>
          <p class="font-body-sm text-[11px] text-outline mt-0.5">当前已生效: 高级学术会员 (Pro) · 剩余 18 天</p>
        </div>
      </div>
    </template>

    <div class="flex flex-col gap-space-md pb-2">
      <!-- 计费周期 -->
      <div class="flex p-1 bg-surface-container rounded-xl gap-1 text-center font-label-md">
        <button
          v-for="c in [
            { key: 'monthly', label: '连续包月' },
            { key: 'quarterly', label: '季付特惠' },
            { key: 'yearly', label: '年度立省 40%' },
          ]"
          :key="c.key"
          type="button"
          class="flex-1 py-1.5 rounded-lg relative transition-all"
          :class="cycle === c.key ? 'bg-surface-container-lowest text-primary font-bold shadow-sm' : 'text-outline hover:text-on-surface'"
          @click="cycle = c.key as typeof cycle"
        >
          {{ c.label }}
          <span
            v-if="c.key === 'yearly'"
            class="absolute -top-2 -right-1 bg-error text-on-error font-label-sm text-[9px] px-1.5 py-0.5 rounded-full shadow-sm leading-tight"
          >
            立减 ¥228
          </span>
        </button>
      </div>

      <!-- SVIP 卡 -->
      <div
        class="relative rounded-2xl p-space-md bg-gradient-to-br from-[#1a1c36] via-[#212444] to-[#131b2e] border-2 border-primary shadow-lg overflow-hidden flex flex-col gap-space-sm"
      >
        <div class="absolute -right-8 -top-8 w-28 h-28 bg-primary/25 rounded-full blur-2xl pointer-events-none" />
        <div
          class="absolute top-0 right-0 bg-gradient-to-l from-tertiary-fixed to-tertiary-fixed-dim text-on-tertiary-fixed text-[11px] font-bold px-3 py-1 rounded-bl-xl shadow-sm flex items-center gap-1"
        >
          <span class="material-symbols-outlined text-[13px] filled">local_fire_department</span>
          学术科研首选
        </div>
        <div class="flex flex-col pr-20">
          <div class="flex items-center gap-1.5">
            <span class="font-headline-md text-[17px] font-bold text-tertiary-fixed">SVIP 旗舰学术版</span>
            <span class="bg-tertiary/60 border border-tertiary-fixed/30 text-tertiary-fixed font-label-sm text-[10px] px-1.5 py-0.5 rounded">
              {{ cycle === 'yearly' ? '年付专享' : '随时升级' }}
            </span>
          </div>
          <p class="font-body-sm text-[12px] text-surface-container-high/80 mt-1">
            专为博硕导师、科研团队与深度硬核学术创作量身打造
          </p>
        </div>
        <div class="flex items-baseline gap-1 my-0.5">
          <span class="text-[14px] text-tertiary-fixed font-bold">¥</span>
          <span class="text-[32px] font-bold text-tertiary-fixed leading-none font-headline-xl">
            {{ price.amount }}
          </span>
          <span class="text-[12px] text-surface-variant/80">
            / {{ cycle === 'yearly' ? '年' : cycle === 'quarterly' ? '季' : '月' }}
          </span>
          <span class="text-[12px] text-outline-variant line-through ml-2">原价 ¥88/月</span>
          <span class="ml-auto bg-primary/30 border border-primary/40 text-primary-fixed font-label-sm text-[11px] px-2 py-0.5 rounded-full">
            {{ price.sub }}
          </span>
        </div>
        <div class="h-[1px] bg-white/10 w-full my-0.5" />
        <div class="grid grid-cols-1 gap-1.5 text-[12px] text-surface-bright">
          <div v-for="f in svipFeatures" :key="f" class="flex items-center gap-2">
            <span class="material-symbols-outlined text-tertiary-fixed text-[16px] shrink-0 filled">check_circle</span>
            <!-- eslint-disable-next-line vue/no-v-html — 静态富文本（<strong> 标记） -->
            <span v-html="f" />
          </div>
        </div>
      </div>

      <!-- Pro 当前套餐 -->
      <div class="rounded-2xl p-space-md bg-surface-container-lowest border border-surface-container-high shadow-sm flex flex-col gap-space-sm">
        <div class="flex items-start justify-between">
          <div>
            <div class="flex items-center gap-2">
              <span class="font-title-sm text-title-sm text-on-surface">标准学术版 (Pro)</span>
              <span class="bg-surface-container-high text-on-surface-variant font-label-sm text-[10px] px-1.5 py-0.5 rounded-full">
                当前使用中
              </span>
            </div>
            <p class="font-body-sm text-[12px] text-outline mt-1">满足日常高频网课与专题讲座转排版需求</p>
          </div>
          <div class="text-right">
            <div class="flex items-baseline gap-0.5 justify-end">
              <span class="text-[13px] text-primary font-bold">¥</span>
              <span class="text-[24px] font-bold text-primary leading-none">19</span>
              <span class="text-[11px] text-outline">/ 月</span>
            </div>
            <span class="text-[10px] text-outline block mt-0.5">连续包月 (次月续费 ¥29)</span>
          </div>
        </div>
        <div class="h-[1px] bg-surface-container w-full" />
        <div class="grid grid-cols-2 gap-y-1.5 gap-x-2 text-[12px] text-on-surface-variant">
          <div v-for="f in proFeatures" :key="f" class="flex items-center gap-1.5">
            <span class="material-symbols-outlined text-primary text-[15px]">check</span>
            <span>{{ f }}</span>
          </div>
        </div>
      </div>

      <!-- 加油包 -->
      <div class="rounded-xl p-space-sm bg-surface-container-low border border-surface-container flex items-center justify-between hover:bg-surface-container transition-colors cursor-pointer">
        <div class="flex items-center gap-2.5 min-w-0">
          <div class="w-8 h-8 rounded-lg bg-surface-container-highest flex items-center justify-center text-primary shrink-0">
            <span class="material-symbols-outlined text-[18px]">bolt</span>
          </div>
          <div class="flex flex-col min-w-0">
            <div class="flex items-center gap-1">
              <span class="font-title-sm text-[13px] text-on-surface">按需算力加油包</span>
              <span class="font-label-sm text-[10px] bg-surface-container text-outline px-1 rounded">单次有效</span>
            </div>
            <span class="font-body-sm text-[11px] text-outline truncate">额外 10 小时转换时长 · 永久不过期</span>
          </div>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <span class="font-title-sm text-[14px] text-on-surface font-bold">¥12</span>
          <span class="material-symbols-outlined text-[16px] text-outline">chevron_right</span>
        </div>
      </div>

      <!-- 保障徽章 -->
      <div class="flex items-center justify-around py-1 text-[11px] text-outline border-t border-surface-container/80 pt-2">
        <div class="flex items-center gap-1">
          <span class="material-symbols-outlined text-[14px] text-primary">verified_user</span>
          <span>随时可退改</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="material-symbols-outlined text-[14px] text-primary">flash_on</span>
          <span>支付即刻开通</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="material-symbols-outlined text-[14px] text-primary">receipt_long</span>
          <span>支持开具普票/专票</span>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="flex flex-col gap-2">
        <div class="flex items-center justify-between px-1">
          <div class="flex items-baseline gap-1">
            <span class="text-[12px] text-outline">应付金额:</span>
            <span class="text-[15px] font-bold text-error">¥</span>
            <span class="text-[24px] font-bold text-error leading-none">{{ price.amount }}</span>
            <span v-if="price.note" class="text-[11px] text-outline ml-1">{{ price.note }}</span>
          </div>
          <span class="text-[11px] text-outline">到期自动续费，可随时取消</span>
        </div>
        <button
          class="w-full py-3.5 px-space-md bg-gradient-to-r from-primary to-secondary hover:brightness-105 active:scale-[0.99] text-on-primary font-title-sm text-title-sm rounded-xl shadow-md shadow-primary/30 flex items-center justify-center gap-2 transition-all"
          @click="emit('pay', cycle), emit('update:modelValue', false)"
        >
          <span class="material-symbols-outlined text-[20px] filled">upgrade</span>
          立即升级至 SVIP 旗舰学术版
        </button>
        <p class="text-[10px] text-center text-outline leading-tight">
          点击支付即代表您同意《视书学术会员服务协议》与《自动续费规则》
        </p>
      </div>
    </template>
  </BottomSheet>
</template>
