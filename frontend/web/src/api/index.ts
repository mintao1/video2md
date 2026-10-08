// 签名层：视图/store 只 import 这些函数。第一版实现来自 mock；
// 换真实 API 时改这里为 live 实现（见 client.ts），上层零改动。
import { impl } from './mock/impl'

export const {
  sendSmsCode,
  loginWithSms,
  loginWithPassword,
  loginWithWechat,
  getBooks,
  toggleFav,
  getToc,
  listRuns,
  createRun,
  cancelRun,
  resumeRun,
  promoteRun,
  deleteRuns,
  getQuota,
  currentUser,
  _setProgress,
  _snapshotRuns,
} = impl
