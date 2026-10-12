// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useIdleLogout } from '../auth/useIdleLogout'
import { ApiError, HttpApi } from '../api/client'
import { UNAUTHORIZED_EVENT, buildCsp, passwordProblems, safeExternalUrl, securityHeaders } from './security'

describe('safeExternalUrl', () => {
  it('chỉ cho http(s), chặn javascript:/data:/vbscript:', () => {
    expect(safeExternalUrl('https://www.youtube.com/watch?v=abc')).toBe('https://www.youtube.com/watch?v=abc')
    expect(safeExternalUrl('http://example.com/a')).toBe('http://example.com/a')
    expect(safeExternalUrl('javascript:alert(1)')).toBeNull()
    expect(safeExternalUrl('JaVaScRiPt:alert(1)')).toBeNull()
    expect(safeExternalUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
    expect(safeExternalUrl('vbscript:msgbox')).toBeNull()
    expect(safeExternalUrl('/relative')).toBeNull()
    expect(safeExternalUrl(undefined)).toBeNull()
  })
})

describe('passwordProblems', () => {
  it('đòi độ dài, đa dạng ký tự và không chứa email/chuỗi dễ đoán', () => {
    expect(passwordProblems('ngan', 'a@x.vn')).toContain('ít nhất 12 ký tự')
    expect(passwordProblems('toanchuthuongdai', 'a@x.vn').some((p) => p.startsWith('kết hợp'))).toBe(true)
    expect(passwordProblems('Nguyenvan123!', 'nguyenvan@x.vn')).toContain('không chứa tên email')
    expect(passwordProblems('Password1234!', 'a@x.vn').some((p) => p.includes('dễ đoán'))).toBe(true)
    expect(passwordProblems('Bao-Mat#Lop7Ky', 'sinhvien@x.vn')).toEqual([])
  })
})

describe('CSP', () => {
  it('chỉ chạy script của app, chỉ kết nối tới app + Supabase, cấm bị nhúng', () => {
    const csp = buildCsp('https://abc.supabase.co/rest', { header: true, https: true })
    expect(csp).toContain("script-src 'self'")
    expect(csp).not.toContain('unsafe-inline')
    expect(csp).not.toContain('unsafe-eval')
    expect(csp).toContain("connect-src 'self' https://abc.supabase.co")
    expect(csp).toContain("object-src 'none'")
    expect(csp).toContain("frame-ancestors 'none'")
    expect(csp).toContain('upgrade-insecure-requests')
    expect(buildCsp(undefined, { header: false, https: false })).not.toContain('frame-ancestors') // không hợp lệ trong <meta>
    const headers = securityHeaders('https://abc.supabase.co', false)
    expect(headers['X-Frame-Options']).toBe('DENY')
    expect(headers['Strict-Transport-Security']).toBeUndefined()
  })
})

describe('useIdleLogout', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
  })
  afterEach(() => vi.useRealTimers())

  it('cảnh báo 1 phút trước rồi đăng xuất; hoạt động thì gia hạn', () => {
    const onIdle = vi.fn()
    const { result } = renderHook(() => useIdleLogout(5 * 60_000, onIdle))
    act(() => vi.advanceTimersByTime(3 * 60_000))
    act(() => window.dispatchEvent(new KeyboardEvent('keydown')))
    act(() => vi.advanceTimersByTime(4 * 60_000 + 1000))
    expect(result.current.remainingMs).not.toBeNull()
    expect(onIdle).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(61_000))
    expect(onIdle).toHaveBeenCalledTimes(1)
  })

  it('hoạt động ở tab khác (localStorage) cũng tính', () => {
    const onIdle = vi.fn()
    renderHook(() => useIdleLogout(2 * 60_000, onIdle))
    act(() => vi.advanceTimersByTime(90_000))
    localStorage.setItem('msks.lastActivity', String(Date.now()))
    act(() => vi.advanceTimersByTime(90_000))
    expect(onIdle).not.toHaveBeenCalled()
  })

  it('timeout 0 tắt tính năng', () => {
    const onIdle = vi.fn()
    renderHook(() => useIdleLogout(0, onIdle))
    act(() => vi.advanceTimersByTime(24 * 3600_000))
    expect(onIdle).not.toHaveBeenCalled()
  })
})

describe('HttpApi 401', () => {
  it('phát sự kiện đăng xuất kèm mã lỗi (vd. phiên bị thu hồi ở thiết bị khác)', async () => {
    const seen: string[] = []
    const listener = (e: Event) => seen.push((e as CustomEvent<string>).detail)
    window.addEventListener(UNAUTHORIZED_EVENT, listener)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ code: 'session_revoked', message: 'x', retryable: false }), { status: 401 })))
    await expect(new HttpApi('/api').listWorkspaces()).rejects.toBeInstanceOf(ApiError)
    expect(seen).toEqual(['session_revoked'])
    window.removeEventListener(UNAUTHORIZED_EVENT, listener)
    vi.unstubAllGlobals()
  })
})
