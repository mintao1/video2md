import { request } from '../client'

/** auto = 项目提供的 LLM；custom = 用户自填的 OpenAI 兼容接口 */
export type LlmMode = 'auto' | 'custom'

export interface LlmSettings {
  base_url: string
  model: string
  has_api_key: boolean
  /** 是否已保存完整的自定义接口（与当前是否在用无关） */
  configured: boolean
  /** 当前生效的模式 */
  mode: LlmMode
  /** 项目提供的模型名（只有名字，不含 URL/Key） */
  system_model: string
}

export function getLlmSettings(): Promise<LlmSettings> {
  return request('/settings/llm')
}

export function saveLlmSettings(body: {
  base_url: string
  api_key?: string
  model: string
}): Promise<LlmSettings> {
  return request('/settings/llm', { method: 'PUT', body: JSON.stringify(body) })
}

export function setLlmMode(mode: LlmMode): Promise<LlmSettings> {
  return request('/settings/llm/mode', { method: 'PUT', body: JSON.stringify({ mode }) })
}

export function clearLlmSettings(): Promise<LlmSettings> {
  return request('/settings/llm', { method: 'DELETE' })
}
