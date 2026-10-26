// fetch 封装占位：第一版不接线；换真实 API 时 live/* 用它。
import type { ApiError } from '../types'

const configuredBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim()

// Production is served behind the same TLS origin as the API. Local Vite
// development uses a separate API port, so keep that default explicit while
// avoiding a localhost API URL in a deployed browser bundle.
const localDevOrigin = typeof window !== 'undefined' &&
  (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')

export const BASE_URL = configuredBaseUrl ||
  (localDevOrigin ? 'http://127.0.0.1:8790' :
    (typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:8790'))

// Google ID token is carried as a Bearer credential for every API request.
// The pre-rename key (v2md_token) is moved over on first read so signed-in users stay signed in.
const TOKEN_KEY = 'v2pdf_token'
const LEGACY_TOKEN_KEY = 'v2md_token'

export function getToken(): string | null {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) return token
  const legacy = localStorage.getItem(LEGACY_TOKEN_KEY)
  if (legacy) {
    localStorage.setItem(TOKEN_KEY, legacy)
    localStorage.removeItem(LEGACY_TOKEN_KEY)
  }
  return legacy
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(LEGACY_TOKEN_KEY)
}

/** Only same-site absolute paths are accepted as post-login targets. */
export function safeRedirect(value: unknown, fallback = '/library'): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) {
    return fallback
  }
  return value
}

// With V2PDF_AUTH=off (development only; production config rejects it) the API
// answers credential-less requests with a fixed dev identity. Ask the backend
// instead of mirroring its auth mode in a build flag. Cached per page load;
// a network failure is not cached so a late-starting API is picked up.
let anonymousSession: Promise<boolean> | null = null

export function hasAnonymousSession(): Promise<boolean> {
  anonymousSession ??= fetch(`${BASE_URL}/me`).then(
    (res) => res.ok,
    () => {
      anonymousSession = null
      return false
    },
  )
  return anonymousSession
}

export class ApiRequestError extends Error {
  code: string | number
  details?: unknown
  constructor(envelope: ApiError['error']) {
    super(envelope.message)
    this.name = 'ApiRequestError'
    this.code = envelope.code
    this.details = envelope.details
  }
}

async function response(path: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken()
  const isFormData = typeof FormData !== 'undefined' && init.body instanceof FormData
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })
  if (!res.ok) {
    if (res.status === 401) {
      clearToken()
      if (typeof window !== 'undefined' && window.location.pathname !== '/') {
        const query = new URLSearchParams({ redirect: window.location.pathname + window.location.search })
        if (token) query.set('reason', 'expired')
        window.location.assign(`/?${query}`)
      }
    }
    let envelope: ApiError['error'] = { code: 'HTTP_' + res.status, message: res.statusText }
    try {
      const body = (await res.json()) as ApiError
      if (body.error) envelope = body.error
    } catch {
      /* keep default */
    }
    throw new ApiRequestError(envelope)
  }
  return res
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await (await response(path, init)).json()) as T
}

export async function requestBlob(path: string): Promise<Blob> {
  return (await response(path)).blob()
}

/** Resume large clips in small authenticated ranges instead of restarting the whole file. */
export async function requestMediaBlob(path: string): Promise<Blob> {
  const parts: Blob[] = []
  const chunkSize = 64 * 1024
  let offset = 0, total = 0, etag = '', type = ''
  while (true) {
    let res: Response | undefined, blob: Blob | undefined
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        res = await response(path, { headers: {
          Range: `bytes=${offset}-${offset + chunkSize - 1}`,
          ...(etag ? { 'If-Range': etag } : {}),
        } })
        blob = await res.blob()
        const length = Number(res.headers.get('Content-Length'))
        if (length && blob.size !== length) throw new TypeError('Incomplete media response')
        break
      } catch (error) {
        if (!(error instanceof TypeError)) throw error
        if (attempt === 1) throw new Error('媒体传输中断，请稍后重试')
      }
    }
    if (!res || !blob) throw new Error('媒体响应为空')
    if (res.status === 200 && offset === 0) return blob // Older servers without range support.
    const range = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(res.headers.get('Content-Range') ?? '')
    const start = Number(range?.[1]), end = Number(range?.[2]), size = Number(range?.[3])
    if (res.status !== 206 || !range || start !== offset || end < start || size <= end ||
        end !== Math.min(offset + chunkSize - 1, size - 1) || blob.size !== end - start + 1 ||
        (total && size !== total) || (etag && res.headers.get('ETag') !== etag)) {
      throw new Error('媒体分段响应不一致，请重新打开本讲')
    }
    total = size
    etag = res.headers.get('ETag') ?? ''
    type = blob.type
    parts.push(blob)
    offset = end + 1
    if (offset === total) return new Blob(parts, { type })
  }
}
