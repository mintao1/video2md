// 签名层：视图/store 只 import 这些函数。第一版实现来自 mock；
// 换真实 API 时改这里为 live 实现（见 client.ts），上层零改动。
// 会员/配额（getQuota/currentUser）已切 live（v2md_membership 真数据），其余留 mock。
import { impl } from './mock/impl'

export { currentUser, getQuota } from './live/profile'

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
  _setProgress,
  _snapshotRuns,
} = impl
