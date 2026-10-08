// API dùng offset theo Unicode code point (ARCHITECTURE.md mục 5.4); chuỗi JS
// đánh chỉ số theo UTF-16. Ký tự ngoài BMP (emoji, một số chữ Hán) chiếm 2 đơn vị UTF-16.

/** Bảng tra code point → UTF-16 cho một văn bản; tạo một lần, tra O(1). */
export class CodePointIndex {
  private readonly offsets: Uint32Array
  readonly length: number
  readonly text: string

  constructor(text: string) {
    this.text = text
    const offsets: number[] = []
    let i = 0
    for (const char of text) {
      offsets.push(i)
      i += char.length
    }
    offsets.push(i)
    this.offsets = Uint32Array.from(offsets)
    this.length = offsets.length - 1
  }

  /** Đổi offset code point sang UTF-16, kẹp vào [0, length]. */
  toUtf16(cp: number): number {
    const clamped = Math.min(Math.max(0, Math.trunc(cp)), this.length)
    return this.offsets[clamped]
  }

  /** Cắt chuỗi theo khoảng code point nửa mở [start, end). */
  slice(start: number, end: number): string {
    return this.text.slice(this.toUtf16(start), this.toUtf16(end))
  }
}

/** Số code point của một chuỗi. */
export function cpLength(text: string): number {
  let n = 0
  for (const _ of text) n++
  return n
}

/** Đổi offset UTF-16 sang code point (dùng khi dựng dữ liệu, không dùng trong render). */
export function utf16ToCp(text: string, index: number): number {
  return cpLength(text.slice(0, index))
}

/** Chia [from, to) thành các đoạn plain / leaf / quote theo khoảng code point. */
export function segment(from: number, to: number, leaf?: [number, number], quote?: [number, number]): [number, number, 'plain' | 'leaf' | 'quote'][] {
  const points = new Set([from, to])
  for (const range of [leaf, quote]) {
    if (!range) continue
    for (const p of range) if (p > from && p < to) points.add(p)
  }
  const sorted = [...points].sort((a, b) => a - b)
  const inside = (p: number, range?: [number, number]) => !!range && p >= range[0] && p < range[1]
  const out: [number, number, 'plain' | 'leaf' | 'quote'][] = []
  for (let i = 0; i < sorted.length - 1; i++) {
    const [a, b] = [sorted[i], sorted[i + 1]]
    if (a === b) continue
    const kind = inside(a, quote) ? 'quote' : inside(a, leaf) ? 'leaf' : 'plain'
    const last = out.at(-1)
    if (last && last[2] === kind) last[1] = b
    else out.push([a, b, kind])
  }
  return out
}
