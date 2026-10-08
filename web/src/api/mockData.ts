// Dữ liệu mẫu cho chế độ mock. Các đoạn văn là trích đoạn mẫu để minh họa giao diện,
// không thay thế tài liệu gốc.
import type { OutlineSection, SourceKind, SourceType } from './types'

export interface SeedSource {
  alias: string
  kind: SourceKind
  title: string
  authors: string[]
  url?: string
  doi?: string
  published_at?: string
  source_type: SourceType
  origin_group_id: string
  grouping_reason?: string
  /** Mỗi phần tử là một trang; `paginated: false` → định dạng không phân trang. */
  pages: string[][]
  paginated: boolean
  failed?: { code: string; message: string }
}

export const SEED_SOURCES: SeedSource[] = [
  {
    alias: 'S1',
    kind: 'pdf',
    title: 'RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval',
    authors: ['P. Sarthi', 'S. Abdullah', 'A. Tuli', 'S. Khanna', 'A. Goldie', 'C. D. Manning'],
    url: 'https://arxiv.org/abs/2401.18059',
    published_at: '2024-01-31',
    source_type: 'peer_reviewed',
    origin_group_id: 'g-raptor',
    paginated: true,
    pages: [
      [
        'Retrieval-augmented language models can better adapt to changes in world state and incorporate long-tail knowledge.',
        'Most existing methods retrieve only short contiguous chunks from a retrieval corpus, limiting holistic understanding of the overall document context.',
      ],
      [
        'RAPTOR recursively embeds, clusters, and summarizes chunks of text, constructing a tree with differing levels of summarization from the bottom up.',
        'At inference time, the model retrieves from this tree, integrating information across lengthy documents at different levels of abstraction.',
      ],
    ],
  },
  {
    alias: 'S2',
    kind: 'pdf',
    title: 'PeerQA: A Scientific Question Answering Dataset from Peer Reviews',
    authors: ['T. Baumgärtner', 'T. Briscoe', 'I. Gurevych'],
    published_at: '2025-04-29',
    source_type: 'peer_reviewed',
    origin_group_id: 'g-peerqa',
    paginated: true,
    pages: [
      [
        'PeerQA is a question answering dataset whose questions are extracted from peer reviews of scientific articles.',
        'The questions have been answered by the paper authors, who also annotated the evidence passages supporting each answer.',
      ],
      [
        'The documents are full scientific articles, considerably longer than the abstracts used in many earlier scientific QA datasets.',
      ],
    ],
  },
  {
    alias: 'S3',
    kind: 'markdown',
    title: 'Báo cáo v11 — kiểm tra độ bền Agreement Ranking',
    authors: ['Nhóm EDAHR'],
    published_at: '2026-10-05',
    source_type: 'user_note',
    origin_group_id: 'g-v11',
    paginated: false,
    pages: [
      [
        '📌 Ghi chú nội bộ — số liệu lấy từ analysis/v11_peerqa_results.md.',
        'Dữ liệu: mteb/PeerQA (NLPeer subset), 70 bài, 136 câu, evidence do tác giả gán; trung vị khoảng 5.200 từ/bài.',
        'Ba nhánh được so sánh: raptor (cây RAPTOR với SBERT, UMAP và tóm tắt BART), all_leaf (rerank toàn bài) và agreement (RRF của cross-encoder và SBERT).',
        'Agreement Ranking không kém RAPTOR: +0,011 Evidence F1 [−0,003; +0,025], Holm p<0,001.',
        'Trên bài dài, RAPTOR đóng gói ít gold nhất ở mọi ngân sách; rerank toàn bài đóng gói nhiều nhất.',
        'Dựng index 70 bài trên RTX 3090: RAPTOR 3.097 s, Agreement 32 s.',
        'Không chứng minh được Agreement tốt hơn rerank toàn bài: +0,006, p=0,22.',
      ],
    ],
  },
  {
    alias: 'S4',
    kind: 'html',
    title: 'Ví dụ blog (dữ liệu mẫu): Vì sao cây tóm tắt thắng trên tài liệu dài',
    authors: ['Blog mẫu'],
    url: 'https://example.com/blog/tree-summaries',
    published_at: '2025-11-02',
    source_type: 'web',
    origin_group_id: 'g-blog',
    paginated: false,
    pages: [
      [
        'In our experience, tree-based summarization always packs more relevant evidence than flat reranking when documents get long.',
        'Summaries let the retriever see the whole paper at once, which flat chunk retrieval cannot do.',
      ],
    ],
  },
  {
    alias: 'S5',
    kind: 'pdf',
    title: 'Báo cáo v11 (bản xuất PDF)',
    authors: ['Nhóm EDAHR'],
    published_at: '2026-10-05',
    source_type: 'user_note',
    origin_group_id: 'g-v11',
    grouping_reason: 'MinHash Jaccard 0,86 ≥ 0,8 với S3 (thuật toán minhash-5shingle v1)',
    paginated: true,
    pages: [
      [
        'Agreement Ranking không kém RAPTOR: +0,011 Evidence F1 [−0,003; +0,025], Holm p<0,001.',
        'Dựng index 70 bài trên RTX 3090: RAPTOR 3.097 s, Agreement 32 s.',
      ],
    ],
  },
  {
    alias: 'S6',
    kind: 'pdf',
    title: 'Bản scan hội thảo 2019',
    authors: [],
    source_type: 'user_note',
    origin_group_id: 'g-scan',
    paginated: true,
    pages: [[]],
    failed: {
      code: 'pdf_no_text_layer',
      message: 'PDF không có lớp văn bản (bản scan). OCR chưa được hỗ trợ trong MVP.',
    },
  },
]

/** Cặp nhóm nguồn đã được người đánh giá xác nhận là độc lập (mục 6.2). */
export const REVIEWED_INDEPENDENT: [string, string][] = [['g-raptor', 'g-v11']]

interface SeedEvidence {
  alias: string
  quote: string
  support: number
  contradiction: number
}

export interface SeedClaim {
  id: string
  text: string
  confidence: number
  section_key: string
  primary: SeedEvidence
  others?: (SeedEvidence & { role: 'corroborating' | 'contradicting' })[]
  /** Đối chứng chéo không chạy hết ngân sách tìm kiếm. */
  crossCheckIncomplete?: boolean
}

export const SEED_CLAIMS: SeedClaim[] = [
  {
    id: 'cl_method',
    text: 'RAPTOR dựng cây bằng cách đệ quy nhúng, phân cụm và tóm tắt các đoạn văn từ dưới lên.',
    confidence: 0.92,
    section_key: 'method',
    primary: {
      alias: 'S1',
      quote: 'RAPTOR recursively embeds, clusters, and summarizes chunks of text, constructing a tree with differing levels of summarization from the bottom up.',
      support: 0.94,
      contradiction: 0.01,
    },
    others: [
      {
        role: 'corroborating',
        alias: 'S3',
        quote: 'Ba nhánh được so sánh: raptor (cây RAPTOR với SBERT, UMAP và tóm tắt BART), all_leaf (rerank toàn bài) và agreement (RRF của cross-encoder và SBERT).',
        support: 0.61,
        contradiction: 0.02,
      },
    ],
  },
  {
    id: 'cl_data',
    text: 'Các câu hỏi trong PeerQA được chính tác giả bài báo trả lời và gán đoạn bằng chứng.',
    confidence: 0.88,
    section_key: 'data',
    primary: {
      alias: 'S2',
      quote: 'The questions have been answered by the paper authors, who also annotated the evidence passages supporting each answer.',
      support: 0.89,
      contradiction: 0.01,
    },
    others: [
      {
        role: 'corroborating',
        alias: 'S3',
        quote: 'Dữ liệu: mteb/PeerQA (NLPeer subset), 70 bài, 136 câu, evidence do tác giả gán; trung vị khoảng 5.200 từ/bài.',
        support: 0.57,
        contradiction: 0.03,
      },
    ],
  },
  {
    id: 'cl_noninf',
    text: 'Trên PeerQA, Agreement Ranking không kém RAPTOR, chênh lệch +0,011 Evidence F1.',
    confidence: 0.9,
    section_key: 'results',
    primary: {
      alias: 'S3',
      quote: 'Agreement Ranking không kém RAPTOR: +0,011 Evidence F1 [−0,003; +0,025], Holm p<0,001.',
      support: 0.96,
      contradiction: 0.01,
    },
  },
  {
    id: 'cl_packing',
    text: 'Trên tài liệu dài của PeerQA, RAPTOR đóng gói ít đoạn gold nhất ở mọi ngân sách đã thử.',
    confidence: 0.81,
    section_key: 'results',
    primary: {
      alias: 'S3',
      quote: 'Trên bài dài, RAPTOR đóng gói ít gold nhất ở mọi ngân sách; rerank toàn bài đóng gói nhiều nhất.',
      support: 0.91,
      contradiction: 0.02,
    },
    others: [
      {
        role: 'contradicting',
        alias: 'S4',
        quote: 'In our experience, tree-based summarization always packs more relevant evidence than flat reranking when documents get long.',
        support: 0.04,
        contradiction: 0.78,
      },
    ],
  },
  {
    id: 'cl_cost',
    text: 'Dựng index 70 bài: RAPTOR mất 3.097 s, Agreement mất 32 s.',
    confidence: 0.86,
    section_key: 'cost',
    primary: {
      alias: 'S3',
      quote: 'Dựng index 70 bài trên RTX 3090: RAPTOR 3.097 s, Agreement 32 s.',
      support: 0.83,
      contradiction: 0.01,
    },
    crossCheckIncomplete: true,
  },
]

export const SEED_REJECTED = [
  {
    id: 'rj_superior',
    text: 'Agreement Ranking tốt hơn rerank toàn bài một cách có ý nghĩa thống kê.',
    reason: 'below_support_threshold',
    section_key: 'results',
    support: 0.11,
  },
  {
    id: 'rj_all_data',
    text: 'Agreement Ranking đã được kiểm chứng trên mọi bộ dữ liệu QA khoa học.',
    reason: 'invalid_or_missing_context_citation',
    section_key: 'results',
    support: null,
  },
]

export const SEED_OUTLINE: OutlineSection[] = [
  { key: 'method', title: 'Phương pháp RAPTOR', questions: ['RAPTOR xây dựng chỉ mục như thế nào?'] },
  { key: 'data', title: 'Bộ dữ liệu PeerQA', questions: ['PeerQA được xây dựng ra sao?'] },
  {
    key: 'results',
    title: 'Kết quả trên tài liệu dài',
    questions: ['Agreement Ranking so với RAPTOR thế nào trên PeerQA?', 'Bộ chọn nào đóng gói nhiều gold hơn?'],
  },
  { key: 'cost', title: 'Chi phí', questions: ['Chi phí dựng index của hai phương pháp khác nhau ra sao?'] },
]

/** Câu dẫn cố định của template báo cáo — không chứa sự kiện (mục 7.3). */
export const TEMPLATE_LEAD = 'Các khẳng định được bằng chứng hỗ trợ cho mục này:'
export const TEMPLATE_EMPTY = 'Không có khẳng định nào qua kiểm chứng cho mục này trong phạm vi nguồn đã chọn.'
export const TEMPLATE_FAILED = 'Câu hỏi con của mục này không hoàn tất; mục được để trống thay vì tạo văn bản bù.'
