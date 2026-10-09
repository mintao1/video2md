// fetch 封装占位：第一版不接线；换真实 API 时 live/* 用它。
import type { ApiError } from '../types'

export const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8790'

export function getToken(): string | null {
  return localStorage.getItem('v2md_token')
}

export class ApiRequestError extends Error {
  code: string
  details?: unknown
  constructor(envelope: ApiError['error']) {
    super(envelope.message)
    this.name = 'ApiRequestError'
    this.code = envelope.code
    this.details = envelope.details
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })
  if (!res.ok) {
    let envelope: ApiError['error'] = { code: 'HTTP_' + res.status, message: res.statusText }
    try {
      const body = (await res.json()) as ApiError
      if (body.error) envelope = body.error
    } catch {
      /* keep default */
    }
    throw new ApiRequestError(envelope)
  }
  return (await res.json()) as T
}
