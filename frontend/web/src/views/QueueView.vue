<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQueueStore, type QueueFilter } from '../stores/queue'
import { useToastStore } from '../stores/toast'
import type { RunListItem } from '../types'
import AppHeader from '../components/common/AppHeader.vue'
import BottomNav from '../components/common/BottomNav.vue'
import FilterChips from '../components/common/FilterChips.vue'
import EmptyState from '../components/common/EmptyState.vue'
import TaskCard from '../components/queue/TaskCard.vue'
import ImportModal from '../components/queue/ImportModal.vue'
import BatchActionBar from '../components/queue/BatchActionBar.vue'
import { uploadFile } from '../api'

const queue = useQueueStore()
const toast = useToastStore()
const route = useRoute()
const router = useRouter()

const importOpen = ref(false)
const quickUrl = ref('')
const submitting = ref(false)
const quickError = ref('')
const importError = ref('')
watch(importOpen, (open) => { if (open) importError.value = '' })

function submissionMessage(error: unknown): string {
  return error instanceof Error && error.message
    ? `提交失败：${error.message}`
    : '提交失败，请稍后重试'
}

function openImportFromRoute(): void {
  if (route.query.openImport !== '1') return
  importOpen.value = true
  void router.replace({ query: {} })
}

const filterOptions = computed(() => [
  { key: 'all', label: '全部任务', count: queue.counts.all },
  { key: 'active', label: '转码中', count: queue.counts.active },
  { key: 'queued', label: '排队中', count: queue.counts.queued },
  { key: 'done', label: '历史完成', count: queue.counts.done },
  { key: 'error', label: '转换失败', count: queue.counts.error },
])

onMounted(async () => {
  try {
    await queue.load()
  } catch {
    toast.show('任务队列暂时无法加载，请检查网络后重试')
  }
  queue.startProgressTick()
  // 从 BottomNav 中央按钮进入时自动打开导入面板
  openImportFromRoute()
})

watch(() => route.query.openImport, openImportFromRoute)

onUnmounted(() => queue.stopProgressTick())

async function quickSubmit() {
  const url = quickUrl.value.trim()
  if (!url) {
    toast.show('请先粘贴视频链接')
    return
  }
  if (submitting.value) return
  quickError.value = ''
  submitting.value = true
  try {
    await queue.createRun(url)
    quickUrl.value = ''
    toast.show('已加入转换队列')
  } catch (error) {
    quickError.value = submissionMessage(error)
  } finally {
    submitting.value = false
  }
}

async function startConvert(payload: { url: string; title?: string; file?: File }) {
  if (submitting.value) return
  importError.value = ''
  submitting.value = true
  try {
    const url = payload.file ? (await uploadFile(payload.file)).path : payload.url
    await queue.createRun(url, payload.title)
    importOpen.value = false
    toast.show('已加入转换队列，稍后可在教材库查看')
  } catch (error) {
    importError.value = submissionMessage(error)
  } finally {
    submitting.value = false
  }
}

function onRead(run: RunListItem) {
  router.push(`/read/${encodeURIComponent(run.run_id)}`)
}

function setFilter(key: string) {
  queue.filter = key as QueueFilter
}
</script>

<template>
  <div class="min-h-screen bg-surface">
    <AppHeader subtitle="Queue Processing" @notifications="toast.show('暂无新通知')" />

    <main class="flex flex-col relative w-full pt-16 pb-nav bg-surface min-h-screen">
      <div class="px-margin pt-space-md pb-space-lg flex flex-col gap-space-lg">
        <!-- 快速导入条 -->
        <div class="bg-surface-container-lowest rounded-xl p-space-md shadow-md border border-outline-variant/30 flex flex-col gap-space-sm">
          <div
            class="flex items-center gap-space-xs bg-surface-container-low rounded-lg p-1.5 border border-outline-variant/40 focus-within:border-primary transition-colors shadow-sm"
          >
            <span class="material-symbols-outlined text-outline text-[18px] ml-1">link</span>
            <input
              v-model="quickUrl"
              :disabled="submitting"
              type="text"
              placeholder="粘贴 B站 / YouTube / 播客等音视频链接..."
              class="bg-transparent flex-1 text-on-surface font-body-md text-body-md focus:outline-none placeholder:text-outline text-[13px] px-1 min-w-0"
              @keydown.enter="quickSubmit"
            />
            <button
              type="button"
              :disabled="submitting"
              :aria-busy="submitting"
              class="px-3 py-1.5 rounded-md bg-primary text-on-primary font-label-md text-label-md font-semibold flex items-center gap-1 shadow-sm active:scale-95 transition-transform shrink-0"
              @click="quickSubmit"
            >
              <span class="material-symbols-outlined text-[16px]">auto_awesome</span>
              <span>{{ submitting ? '正在解析…' : '开始解析' }}</span>
            </button>
          </div>
          <p v-if="submitting && !importOpen" role="status" aria-live="polite" class="text-sm text-on-surface-variant">
            正在读取视频与合集信息，请稍候。解析成功后会自动加入队列。
          </p>
          <p v-if="quickError" role="alert" class="text-sm text-red-700">{{ quickError }}</p>
          <div class="flex items-center justify-between pt-0.5 px-0.5">
            <div class="flex items-center gap-1 text-[11px] text-outline font-label-sm">
              <span class="material-symbols-outlined text-[14px] text-primary">bolt</span>
              <span>支持合集、主页处理</span>
            </div>
            <button
              type="button"
              :disabled="submitting"
              class="px-2.5 py-1 rounded-md bg-surface-container text-on-surface-variant hover:text-on-surface font-label-sm text-[12px] font-semibold flex items-center gap-1 border border-outline-variant/30 active:scale-95 transition-transform shrink-0"
              @click="importOpen = true"
            >
              <span class="material-symbols-outlined text-[15px] text-primary">upload_file</span>
              <span>更多导入方式</span>
            </button>
          </div>
        </div>

        <!-- 状态筛选 -->
        <FilterChips :options="filterOptions" :model-value="queue.filter" @update:model-value="setFilter" />

        <!-- 队列头 + 批量管理 -->
        <div class="flex flex-col gap-space-md">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-space-xs">
              <span class="material-symbols-outlined text-[20px] text-primary">stream</span>
              <span class="font-title-sm text-title-sm text-on-surface">实时流水线队列</span>
            </div>
            <div class="flex items-center gap-space-sm">
              <span class="font-label-sm text-[11px] text-outline flex items-center gap-0.5">
                <span class="w-1.5 h-1.5 rounded-full bg-secondary animate-ping" />
                自动刷新 · 3s
              </span>
              <button
                v-if="!queue.batchMode"
                class="flex items-center gap-0.5 px-2.5 py-1 rounded-full bg-primary/10 text-primary font-label-sm text-label-sm font-semibold transition-all active:scale-95"
                @click="queue.batchMode = true"
              >
                <span class="material-symbols-outlined text-[16px]">checklist</span>
                <span>批量管理</span>
              </button>
              <button
                v-else
                class="font-label-sm text-label-sm text-on-surface-variant font-semibold active:opacity-75"
                @click="queue.exitBatchMode()"
              >
                退出批量
              </button>
            </div>
          </div>

          <BatchActionBar v-if="queue.batchMode" />

          <!-- 任务卡列表 -->
          <TaskCard
            v-for="(run, i) in queue.filtered"
            :key="run.run_id"
            :run="run"
            :hue="(220 + i * 37) % 360"
            @read="onRead"
          />

          <EmptyState
            v-if="queue.filtered.length === 0 && !queue.loading"
            icon="motion_photos_on"
            title="该筛选下暂无任务"
            description="粘贴一个网课链接，视书会自动转录、选帧并排版成教材。"
            action-label="新建转换"
            @action="importOpen = true"
          />
        </div>
      </div>
    </main>

    <ImportModal v-model="importOpen" :error="importError" :busy="submitting" @start="startConvert" @draft="toast.show('草稿已保存')" />
    <BottomNav />
  </div>
</template>
