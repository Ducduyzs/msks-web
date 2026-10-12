import { useCallback, useEffect, useRef, useState } from 'react'

const KEY = 'msks.lastActivity'
const EVENTS = ['pointerdown', 'keydown', 'wheel', 'touchstart', 'scroll'] as const
export const IDLE_WARNING_MS = 60_000

function readShared(): number {
  try {
    return Number(localStorage.getItem(KEY)) || 0
  } catch {
    return 0
  }
}

function writeShared(at: number) {
  try {
    localStorage.setItem(KEY, String(at))
  } catch {
    /* chế độ riêng tư: chỉ tính hoạt động của tab này */
  }
}

/**
 * Tự đăng xuất sau `timeoutMs` không hoạt động (máy dùng chung, quên đăng xuất). Hoạt động ở tab khác cũng tính
 * (đồng bộ qua localStorage). Báo trước 1 phút; `stay()` gia hạn. `timeoutMs` = 0 tắt tính năng.
 */
export function useIdleLogout(timeoutMs: number, onIdle: () => void) {
  const last = useRef(0) // đặt trong effect khi bật
  const [remainingMs, setRemainingMs] = useState<number | null>(null)
  const fired = useRef(false)

  const touch = useCallback(() => {
    const now = Date.now()
    // Ghi tối đa mỗi 5 s để không ghi localStorage liên tục khi cuộn/di chuột.
    if (now - last.current > 5000) writeShared(now)
    last.current = now
  }, [])

  useEffect(() => {
    if (!timeoutMs) return
    last.current = Date.now()
    writeShared(last.current)
    fired.current = false
    for (const name of EVENTS) window.addEventListener(name, touch, { passive: true })
    const tick = setInterval(() => {
      const latest = Math.max(last.current, readShared())
      const left = timeoutMs - (Date.now() - latest)
      if (left <= 0) {
        if (!fired.current) {
          fired.current = true
          setRemainingMs(null)
          onIdle()
        }
      } else {
        setRemainingMs(left <= IDLE_WARNING_MS ? left : null)
      }
    }, 1000)
    return () => {
      clearInterval(tick)
      for (const name of EVENTS) window.removeEventListener(name, touch)
    }
  }, [timeoutMs, onIdle, touch])

  const stay = useCallback(() => {
    last.current = Date.now()
    writeShared(last.current)
    setRemainingMs(null)
  }, [])

  return { remainingMs, stay }
}
