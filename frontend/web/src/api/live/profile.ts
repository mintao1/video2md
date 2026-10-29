// 会员/配额展示档的 live 实现（首个切真数据的面）：
// GET /me、GET /quota——后端 v2pdf_membership（PG）；PG 关时回 free/全 0 默认档。
// 个人资料生产 API 实现。
import { request } from '../client'
import type { CostReport, Quota, User } from '../../types'

export async function currentUser(): Promise<User> {
  return request<User>('/me')
}

export async function getQuota(): Promise<Quota> {
  return request<Quota>('/quota')
}

export async function getCost(): Promise<CostReport> {
  return request<CostReport>('/cost')
}
