import { HttpApi, type Api } from './client'
import { MockApi } from './mock'

// VITE_API_MODE=http dùng backend thật qua VITE_API_BASE (mặc định /api, proxy tới FastAPI).
const mode = import.meta.env.VITE_API_MODE ?? 'mock'

export const api: Api = mode === 'http' ? new HttpApi(import.meta.env.VITE_API_BASE ?? '/api') : new MockApi()

export * from './types'
export { ApiError, newIdempotencyKey, type Api, type StreamHandle } from './client'
