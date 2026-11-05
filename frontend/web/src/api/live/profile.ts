// 会员/配额展示档的 live 实现（首个切真数据的面）：
// GET /me、GET /quota——后端 membership table（PG）；PG 关时回 free/全 0 默认档。
// 个人资料生产 API 实现。
import { request } from '../client'
import type { CostReport, Quota, User } from '../../types'

export const TERMS_VERSION = '2026-10-21'

export async function currentUser(): Promise<User> {
  return request<User>('/me')
}

export async function getQuota(): Promise<Quota> {
  return request<Quota>('/quota')
}

export async function getCost(): Promise<CostReport> {
  return request<CostReport>('/cost')
}

/** Record the exact public agreement version accepted on the brand page. */
export async function acceptTerms(): Promise<{
  terms_version: string | null
  terms_accepted_at: string | null
  terms_current: boolean
}> {
  return request('/auth/consent', {
    method: 'POST',
    body: JSON.stringify({ terms_version: TERMS_VERSION }),
  })
}
