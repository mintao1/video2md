import { request } from '../client'

export interface LlmSettings {
  base_url: string
  model: string
  has_api_key: boolean
  configured: boolean
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

export function clearLlmSettings(): Promise<LlmSettings> {
  return request('/settings/llm', { method: 'DELETE' })
}
