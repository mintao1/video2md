// 生产 API 出口：视图/store 只从这里导入业务能力。
// 会员/配额（getQuota/currentUser，v2md_membership）、
// 教材库（getBooks/toggleFav/getToc/getNotes/saveProgress，/runs+/prefs）、
// 队列页主链（listRuns/createRun/cancelRun → GET/POST /runs）。
export { currentUser, getQuota, getCost } from './live/profile'
export { getLlmSettings, saveLlmSettings, clearLlmSettings } from './live/llm-settings'
export { getBooks, toggleFav, getToc, getNotes, getMedia, saveProgress } from './live/books'
export { listRuns, createRun, cancelRun, resumeRun, promoteRun, deleteRuns, downloadBundle } from './live/runs'
export { uploadFile } from './live/uploads'

// 登录由 router + auth store 处理；业务 API 统一复用 client 的 Bearer token。
