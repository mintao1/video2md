// 签名层：视图/store 只 import 这些函数。第一版实现来自 mock；
// 换真实 API 时改这里为 live 实现（见 client.ts），上层零改动。
// 已切 live：会员/配额（getQuota/currentUser，v2md_membership）、
// 教材库（getBooks/toggleFav/getToc/getNotes/saveProgress，/runs+/prefs）、
// 队列页主链（listRuns/createRun/cancelRun → GET/POST /runs）。
export { currentUser, getQuota } from './live/profile'
export { getLlmSettings, saveLlmSettings, clearLlmSettings } from './live/llm-settings'
export { getBooks, toggleFav, getToc, getNotes, getMedia, saveProgress } from './live/books'
export { listRuns, createRun, cancelRun, resumeRun, promoteRun, deleteRuns, downloadBundle } from './live/runs'

// 登录由 router + auth store 处理；业务 API 统一复用 client 的 Bearer token。
