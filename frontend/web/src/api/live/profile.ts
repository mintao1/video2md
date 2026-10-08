// 会员/配额展示档的 live 实现（首个切真数据的面）：
// GET /me、GET /quota——后端 v2md_membership（PG）；PG 关时回 free/全 0 默认档。
// 签名与 mock/impl.ts 同构，view/store 零改动。
import { request } from '../client'
import type { Quota, User } from '../../types'

export async function currentUser(): Promise<User> {
  return request<User>('/me')
}

export async function getQuota(): Promise<Quota> {
  return request<Quota>('/quota')
}
