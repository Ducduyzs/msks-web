// Bài giảng mẫu cho chế độ mock. Bản chép lấy từ lần chạy pipeline thật trên video TTS tiếng Việt
// (analysis/lecture_vi_e2e.json, 09/10/2026) — giữ nguyên lỗi ASR ("Argument Ranking", "ca" thay "k") để
// giao diện sửa bản chép có thứ thật để sửa. Chế độ mock không xử lý video: mọi video tải lên đều nhận bản này.
import type { FrameRegion, MediaAssetRef, MediaCapabilities, MediaCoverage, SourceContent, Timeline, TranscriptSegment } from './types'

export const LECTURE_DURATION_MS = 53_400
export const FRAME_SIZE = { width: 1280, height: 720 }

const SPEECH: [number, number, string, string[]][] = [
  [300, 5620, 'Hôm nay chúng ta học về Argument Ranking, một phương pháp xếp hạng đoạn văn cho hệ thống hỏi đáp.', []],
  [7200, 13080, 'Phương pháp này kết hợp thứ hạng của Cross Encoder với thứ hạng của SBRT bằng Reciport Run Fusion.', []],
  [14860, 17160, 'Hằng số ca trong công thức được đặt bằng 60.', ['low_confidence']],
  [18760, 22440, 'Khác với Raptor, Argument Ranking không cần dựng cây tóm tắt.', []],
  [22440, 30500, 'Với 70 bài báo, thời gian lập chỉ mục chỉ mất 32 giây, trong khi Raptor mất khoảng 3.097 giây', []],
  [30500, 37160, 'Trên tập dữ liệu PETK, Agamem Ranking không kém Raptor về độ chính xác của bằng chứng', []],
  [37160, 43260, 'Tuy nhiên, phương pháp này chưa được chứng minh là tốt hơn việc xếp hạng lại toàn bộ bài báo', []],
  [44780, 49680, 'Với bài giảng tiếng Việt, chúng ta cần đánh giá lại mô hình kiểm chứng trước khi tin vào kết quả', []],
]

/** Mỗi slide: thời điểm xuất hiện và các dòng chữ OCR đọc được. */
const SLIDES: [number, number, string[]][] = [
  [0, 20_000, ['Agreement Ranking', 'RRF giữa cross-encoder và SBERT', 'Hằng số k = 60']],
  [20_000, 35_000, ['Chi phí lập chỉ mục', 'Không dựng cây tóm tắt', '70 bài: 32 giây so với 3097 giây']],
  [35_000, 45_000, ['Kết quả trên PeerQA', 'Không kém RAPTOR']],
  [45_000, LECTURE_DURATION_MS, ['Kết quả trên PeerQA', 'Không kém RAPTOR', 'Chưa chứng minh tốt hơn rerank toàn bài']],
]

const lineBox = (line: number): [number, number, number, number] =>
  line === 0 ? [80, 70, 1200, 170] : [100, 200 + (line - 1) * 120, 1180, 290 + (line - 1) * 120]

export interface LectureData {
  segments: TranscriptSegment[]
  regions: FrameRegion[]
  assets: MediaAssetRef[]
}

export function sampleLecture(sourceId: string, captionCues?: { start_ms: number; end_ms: number; text: string }[]): LectureData {
  const segments: TranscriptSegment[] = captionCues
    ? captionCues.map((c, i) => ({
        id: `${sourceId}_seg${i}`,
        start_ms: c.start_ms,
        end_ms: c.end_ms,
        text: c.text,
        origin: 'human_caption',
        flags: [],
        reviewed: false,
        audio_asset_id: null,
      }))
    : SPEECH.map(([start, end, text, flags], i) => ({
        id: `${sourceId}_seg${i}`,
        start_ms: start,
        end_ms: end,
        text,
        origin: 'asr',
        flags,
        reviewed: false,
        audio_asset_id: null,
      }))
  const assets: MediaAssetRef[] = []
  const regions: FrameRegion[] = []
  SLIDES.forEach(([from, to, lines], slide) => {
    const assetId = `${sourceId}_frame${slide}`
    assets.push({ id: assetId, kind: 'frame', mime: 'image/svg+xml', meta: { timestamp_ms: from, ...FRAME_SIZE, scale: 1 } })
    lines.forEach((text, line) =>
      regions.push({
        id: `${sourceId}_reg${slide}_${line}`,
        frame_asset_id: assetId,
        timestamp_ms: from,
        visible_from_ms: from,
        visible_to_ms: to,
        bbox: lineBox(line),
        text,
        method: 'ocr',
        confidence: 0.74,
        included: true,
        flags: [],
        review_state: 'automatic',
      }),
    )
  })
  return { segments, regions, assets }
}

/** Canonical text như pipeline: lời giảng và chữ trên bảng thành các đoạn lá riêng. */
export function lectureContent(sourceId: string, parseRevisionId: string, data: LectureData): SourceContent {
  const blocks = [...data.segments.map((s) => s.text), ...unique(data.regions.filter((r) => r.included).map((r) => r.text))]
  return { source_id: sourceId, parse_revision_id: parseRevisionId, text: blocks.join('\n\n'), pages: null }
}

function unique(items: string[]) {
  return [...new Set(items)]
}

export function lectureCoverage(data: LectureData, captions: boolean, durationMs = LECTURE_DURATION_MS): MediaCoverage {
  const flagged = data.segments.filter((s) => s.flags.some((f) => ['low_confidence', 'possible_repetition', 'alignment_uncertain'].includes(f))).length
  const speech = data.segments.reduce((sum, s) => sum + (s.end_ms - s.start_ms), 0)
  return {
    duration_ms: durationMs,
    speech_ratio: Number((speech / durationMs).toFixed(4)),
    segments: data.segments.length,
    segments_flagged: flagged,
    flagged_ratio: data.segments.length ? Number((flagged / data.segments.length).toFixed(4)) : null,
    segments_dropped_as_non_speech: captions ? 0 : 1,
    frames_selected: data.assets.length,
    board_regions: data.regions.filter((r) => r.included).length,
    board_regions_low_confidence: 0,
    visual_incomplete_from_ms: null,
    audio_read: !captions,
    captions_used: captions,
    visual_read: data.assets.length > 0,
  }
}

export function buildTimeline(
  sourceId: string,
  parseRevisionId: string,
  data: LectureData,
  capabilities: MediaCapabilities,
  coverage: MediaCoverage,
  parent: string | null,
): Timeline {
  return {
    source_id: sourceId,
    parse_revision_id: parseRevisionId,
    extraction_revision_id: `er_${parseRevisionId}`,
    parent_extraction_revision_id: parent,
    duration_ms: coverage.duration_ms ?? null,
    capabilities,
    coverage,
    versions: { asr: coverage.captions_used ? null : { model: 'mobiuslabsgmbh/faster-whisper-large-v3-turbo (mock)', glossary_sha256: null }, ocr: { engine: 'easyocr (mock)' } },
    segments: data.segments,
    regions: data.regions,
    assets: data.assets,
  }
}

/** Ảnh slide giả lập (SVG) đặt chữ đúng vị trí bbox của vùng OCR. */
export function frameDataUrl(data: LectureData, assetId: string) {
  const regions = data.regions.filter((r) => r.frame_asset_id === assetId)
  const escape = (t: string) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const texts = regions
    .map((r, i) => {
      const [x0, , , y1] = r.bbox
      const size = i === 0 ? 64 : 48
      return `<text x="${x0}" y="${y1 - 24}" font-family="sans-serif" font-size="${size}" font-weight="${i === 0 ? 700 : 400}" fill="#1f2937">${escape(r.text)}</text>`
    })
    .join('')
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${FRAME_SIZE.width}" height="${FRAME_SIZE.height}" viewBox="0 0 ${FRAME_SIZE.width} ${FRAME_SIZE.height}"><rect width="100%" height="100%" fill="#fbfaf7"/><rect x="0" y="0" width="100%" height="12" fill="#3b6ea5"/>${texts}</svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

/** Đọc SRT/WebVTT tối thiểu cho mock (backend dùng media/captions.py, kiểm tra chặt hơn). */
export function parseCaptions(text: string) {
  const toMs = (t: string) => {
    const match = t.trim().match(/^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$/)
    if (!match) return null
    const [, h = '0', m, s, ms] = match
    return ((Number(h) * 60 + Number(m)) * 60 + Number(s)) * 1000 + Number(ms.padEnd(3, '0'))
  }
  const cues: { start_ms: number; end_ms: number; text: string }[] = []
  for (const block of text.replace(/\r/g, '').split(/\n{2,}/)) {
    const lines = block.split('\n')
    const timing = lines.findIndex((l) => l.includes('-->'))
    if (timing < 0) continue
    const [a, b] = lines[timing].split('-->')
    const start = toMs(a)
    const end = toMs(b.trim().split(/\s+/)[0])
    const body = lines.slice(timing + 1).join(' ').replace(/<[^>]+>/g, '').trim()
    if (start !== null && end !== null && end > start && body) cues.push({ start_ms: start, end_ms: end, text: body })
  }
  return cues
}
