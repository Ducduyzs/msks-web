import { describe, expect, it } from 'vitest'
import { CodePointIndex, cpLength, segment, utf16ToCp } from './codepoints'

describe('CodePointIndex', () => {
  it('cắt đúng theo code point khi có emoji (2 đơn vị UTF-16)', () => {
    const text = '📌 Ghi chú: Agreement'
    const index = new CodePointIndex(text)
    expect(index.length).toBe(cpLength(text))
    expect(index.length).toBe(text.length - 1)
    // "Ghi" bắt đầu ở code point 2 (emoji=1, khoảng trắng=1) nhưng ở UTF-16 index 3.
    expect(index.slice(2, 5)).toBe('Ghi')
    expect(index.toUtf16(2)).toBe(3)
  })

  it('giữ nguyên dấu tiếng Việt dạng tổ hợp (NFD) như các code point riêng', () => {
    const nfd = 'Việt'
    const index = new CodePointIndex(nfd)
    expect(index.length).toBe(6)
    expect(index.slice(0, 6)).toBe(nfd)
  })

  it('kẹp offset ngoài phạm vi', () => {
    const index = new CodePointIndex('abc')
    expect(index.slice(-5, 99)).toBe('abc')
  })

  it('utf16ToCp là nghịch đảo của toUtf16 tại ranh giới ký tự', () => {
    const text = 'a😀b😀c'
    const index = new CodePointIndex(text)
    for (let cp = 0; cp <= index.length; cp++) expect(utf16ToCp(text, index.toUtf16(cp))).toBe(cp)
  })
})

describe('segment', () => {
  it('chia plain / leaf / quote lồng nhau', () => {
    expect(segment(0, 20, [5, 15], [8, 12])).toEqual([
      [0, 5, 'plain'],
      [5, 8, 'leaf'],
      [8, 12, 'quote'],
      [12, 15, 'leaf'],
      [15, 20, 'plain'],
    ])
  })

  it('bỏ qua khoảng nằm ngoài trang hiện tại', () => {
    expect(segment(0, 10, [30, 40], [32, 35])).toEqual([[0, 10, 'plain']])
  })

  it('cắt khoảng vắt qua ranh giới trang', () => {
    expect(segment(10, 20, [5, 15], [12, 25])).toEqual([
      [10, 12, 'leaf'],
      [12, 20, 'quote'],
    ])
  })
})
