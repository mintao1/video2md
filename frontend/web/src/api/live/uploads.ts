import { request } from '../client'

interface UploadResponse {
  path: string
  size: number
  name: string
}

export async function uploadFile(file: File): Promise<UploadResponse> {
  const body = new FormData()
  body.append('file', file, file.name)
  return request<UploadResponse>('/uploads', { method: 'POST', body })
}
