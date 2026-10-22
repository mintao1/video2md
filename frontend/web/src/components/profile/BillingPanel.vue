<script setup lang="ts">
import type { CostReport } from '../../types'
defineProps<{ report: CostReport | null; loading?: boolean; error?: string }>()
function money(value: number | null | undefined) {
  return value == null ? '待结算' : `$${value.toFixed(6)}`
}
</script>

<template>
  <section aria-label="费用明细" class="rounded-xl p-space-md bg-surface-container-lowest flex flex-col gap-space-sm">
    <h2 class="font-title-sm text-title-sm">费用明细</h2>
    <p v-if="loading" role="status">正在加载费用明细…</p>
    <p v-else-if="error" role="alert" class="text-error">{{ error }}</p>
    <template v-else-if="report">
      <p>本月成本归因 {{ money(report.spend_usd) }} · {{ report.usage.runs_n }} 个任务</p>
      <p v-if="!report.pricing_configured" role="alert" class="text-error">服务尚未完成定价，零费用不代表没有成本。</p>
      <p class="text-body-sm text-outline">{{ report.gpu_policy === 'free' ? '免费 GPU：费用 $0，保留实际用量。' : 'GPU 按配置的分钟单价计算。' }} 用量成本归因以服务商最终账单为准。</p>
      <p class="text-body-sm">当前单价（USD）：输入 {{ report.prices.llm_in_mtok }}/百万 tokens，缓存输入 {{ report.prices.llm_cached_in_mtok ?? 0 }}/百万 tokens，输出 {{ report.prices.llm_out_mtok }}/百万 tokens，GPU {{ report.prices.gpu_min }}/分钟。</p>
      <p class="text-body-sm">月度合计：输入 {{ report.usage.llm_in_tok }} tokens，输出 {{ report.usage.llm_out_tok }} tokens，GPU {{ report.usage.gpu_s.toFixed(3) }} 秒。</p>
      <p v-if="!report.runs.length">本月暂无任务。</p>
      <article v-for="run in report.runs" :key="run.run_id" class="border-t border-outline-variant/30 pt-space-sm text-body-sm flex flex-col gap-1">
        <p>{{ run.title }} · {{ run.status }} <span v-if="run.deleted">（任务已删除，账本保留）</span></p>
        <p class="break-all">任务编号：{{ run.run_id }}</p>
        <p v-if="run.usage">输入 {{ run.usage.llm_in_tok }} tokens（缓存 {{ run.usage.llm_cached_in_tok ?? 0 }}），输出 {{ run.usage.llm_out_tok }} tokens，GPU {{ run.usage.gpu_s.toFixed(3) }} 秒。</p>
        <p v-else>用量尚未结算</p>
        <p>费用 {{ money(run.cost_usd) }}<span v-if="run.cost_usd != null && !run.cost_pricing"> · 历史记录未保存结算单价</span></p>
      </article>
    </template>
  </section>
</template>
