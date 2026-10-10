# Đánh giá lời giảng tiếng Việt — 09/10/2026

Phạm vi: profile `lecture_vi_v1` (LECTURE_ARCHITECTURE.md mục 7, 11). Ba lớp đo tách riêng:
**chép lời** (ASR) → **pipeline bài giảng** (upload → transcript → OCR → index) → **hỗ trợ claim** (NLI + chốt).
Máy đo: GPU 6 GB, Windows. Số liệu thô nằm trong các file `analysis/*.json` liệt kê ở cuối.

## 1. Kết luận

| Câu hỏi | Trả lời | Căn cứ |
|---|---|---|
| ASR mặc định cho tiếng Việt? | **Giữ `faster-whisper-large-v3-turbo`**, không đặt `asr_model_vi` | Mục 2–3 |
| PhoWhisper có tốt hơn không? | Trên tiếng Việt đọc thuần: **có** (WER −2,2 điểm, có ý nghĩa thống kê). Trên bài giảng có thuật ngữ Anh: **kém hơn**, và **không có timestamp dùng được** (cửa sổ ~30 s) → citation không tua đúng chỗ | Mục 2, 3 |
| Glossary (`initial_prompt`) có đáng dùng? | **Có**: WER bài giảng mẫu 5,8 % → 3,6 % (sạch), 6,5 % → 4,35 % (nhiễu) | Mục 3 |
| Pipeline tiếng Việt chạy đúng end-to-end? | **12/12 kiểm tra đạt** sau 2 lỗi đã sửa trong đợt này | Mục 4, 5 |
| NLI tiếng Việt đủ tin để publish? | mDeBERTa + chuẩn hóa số + chốt: precision 1,0 / recall 0,889 trên 40 cặp — **số này lạc quan** vì chốt được viết sau khi xem lỗi trên chính bộ này. Cần tập held-out độc lập trước khi coi là đã hiệu chỉnh | Mục 6 |

## 2. ASR trên FLEURS vi_vn (dev, 120 câu đầu, 24,5 phút, CC BY 4.0)

Cùng 120 câu cho mọi model; beam 5, `language=vi`, không VAD, `condition_on_previous_text=False`.
Chuẩn hóa: NFKC, chữ thường, bỏ dấu câu (giữ `%`), gộp dấu nghìn (`3.097` = `3097`).
Cột *chữ số*: đổi số viết bằng chữ → chữ số ở **cả hai phía** trước khi chấm (`msks.vi_text.words_to_digits`) —
cần thiết vì PhoWhisper chép “hai mươi” còn FLEURS ghi “20”; không đổi thì PhoWhisper bị phạt oan.

| Model | WER | WER (chữ số) | CER (chữ số) | Giữ đúng số (chữ số) | RTF | VRAM đỉnh |
|---|---|---|---|---|---|---|
| `faster-whisper-large-v3-turbo` (CT2 int8_fp16) | 8,09 % | 7,78 % | 4,38 % | 100 % | **0,09** | **1,3 GB** |
| `faster-whisper-medium` (CT2) | 12,48 % | 12,21 % | 7,17 % | 96 % | 0,24 | 1,3 GB |
| `vinai/PhoWhisper-medium` (HF fp16) | 9,37 % | 7,64 % | 4,03 % | 89 % | 0,65 | 2,5 GB |
| `vinai/PhoWhisper-large` (HF fp16) | 7,37 % | **5,52 %** | **3,19 %** | 89 % | 0,82 | 4,8 GB |
| PhoWhisper-large → CT2 (fp16, int8_fp16 khi chạy) | 6,73 % | 5,55 % | 3,19 % | 93 % | 0,33 | 2,5 GB |

- **Chênh lệch turbo − PhoWhisper-large-CT2** (WER chữ số, bootstrap ghép cặp theo câu, 5 000 lần):
  +2,23 điểm, **KTC 95 % [1,35; 3,14]**; PhoWhisper tốt hơn ở 69 câu, kém hơn ở 23, hòa 28.
- Chuyển sang CTranslate2 giữ nguyên độ chính xác (5,52 % → 5,55 %), nhanh hơn 2,5 lần, VRAM giảm một nửa.
  Vẫn chậm hơn turbo 3,6 lần (bài giảng 60 phút: ~20 phút so với ~5,5 phút trên GPU này).
- “Giữ đúng số” của PhoWhisper < 100 % chủ yếu do bộ chấm: “thứ bảy” (thứ tự) cố ý **không** đổi thành 7
  vì trùng “thứ Bảy” và “một phương pháp”.
- Lỗi turbo hay gặp: tên nước ngoài phiên âm (“phi líp pin” ↔ “philippines”); **nhập âm giọng Nam** (ch/tr:
  “chinh” → “trinh”; v/d: “vụ” → “dụ”; hỏi/ngã: “dậy” → “dạy”); thanh điệu (“mồi” → “mòi”).
- Toàn bộ 361 câu dev (71,7 phút, chỉ turbo và medium, chuẩn hóa cũ NFC): turbo 8,35 % / medium 12,3 %
  (`asr_vi_fleurs_dev_ct2.json`) — nhất quán với tập 120 câu.

## 3. ASR trên bài giảng mẫu (53,4 s, 8 câu, TTS `vi-VN-HoaiMyNeural`, có thuật ngữ Anh)

Gọi trực tiếp `msks.media.asr.transcribe` (VAD bật, như pipeline). Bản *nhiễu*: thêm tiếng ồn nền.

| | turbo | turbo + glossary | PhoWhisper-CT2 | PhoWhisper-CT2 + glossary |
|---|---|---|---|---|
| WER chữ số — sạch | 5,8 % | **3,6 %** | 6,5 % | 5,1 % |
| WER chữ số — nhiễu | 6,5 % | **4,35 %** | 8,0 % | 5,8 % |
| Số segment | 8 | 8 | **2** | **2** |
| Lệch đầu câu p50 / max | 188 / 1 500 ms | — | **6,4–14,4 s / tới 23,6 s** | |

Glossary thử: `Agreement Ranking, cross-encoder, SBERT, reciprocal rank fusion, RAPTOR, PeerQA, hằng số k.`
— sửa được “ca” → “k”, “SBRT” → “SBERT”, “Raptor” → “RAPTOR”; “Agreement” vẫn thành “argument/Agamem”.

**Vì sao không chọn PhoWhisper dù FLEURS tốt hơn:**

1. Không sinh timestamp theo câu (trả 1 segment cho mỗi cửa sổ 30 s, có segment độ dài vô lý như 300–560 ms
   cho 30 s lời). Citation bài giảng dựa vào `start_ms/end_ms` để tua video; lệch 6–24 s là sai chỗ.
2. Kém hơn trên thuật ngữ tiếng Anh xen kẽ — đúng loại nội dung của bài giảng kỹ thuật.
3. Không dấu câu, chữ thường, số viết bằng chữ → tách câu/chunk kém, tìm theo con số (“32”) không khớp.
4. Chậm hơn 3,6 lần.

Muốn dùng PhoWhisper phải thêm bước forced alignment riêng (ví dụ wav2vec2 tiếng Việt) và inverse text
normalization — chỉ đáng làm nếu bài giảng thật cho thấy turbo không đạt.

## 4. Lỗi tìm thấy khi đo và đã sửa

| Lỗi | Bằng chứng | Sửa | Test |
|---|---|---|---|
| **Whisper bịa câu kết YouTube** ở đuôi im lặng: “Hẹn gặp lại các bạn trong những video tiếp theo.” / “Cảm ơn các bạn đã theo dõi.” — bị index và có thể bị trích dẫn | avg_logprob −0,27, no_speech 0,0 → ngưỡng tin cậy cũ không bắt; segment 49,7–**79,7 s** trong video 53,4 s | `asr.screen_segment`: bỏ segment bắt đầu sau cuối media hoặc chỉ gồm câu bịa quen thuộc; segment vượt cuối media bị kẹp + cờ `beyond_media_end`; câu bịa lẫn trong câu thật → cờ `stock_phrase`. Đếm vào `dropped` | `test_asr_screen_segment` (7 ca), e2e |
| **Claim tiếng Anh cho câu hỏi tiếng Việt**, và “3.097 giây” (dấu nghìn kiểu Việt) bị chép vào câu tiếng Anh → đọc thành *ba giây* | e2e lần 1: “RAPTOR takes approximately 3.097 seconds” | `qa.LANGUAGE_RULES` (trả lời cùng ngôn ngữ câu hỏi, chép số nguyên văn) — **chỉ** cho profile không phải `en`; prompt profile `product` giữ đúng hợp đồng v11 | `test_prompt_language_rules_only_for_non_english_profiles`, e2e |
| Bộ chấm: “3.097” ≠ “3097” | WER sạch 14,5 % → 5,8 % sau khi sửa cùng lỗi trên | `normalize_vi` gộp dấu nghìn (nhóm đúng 3 chữ số) | `test_asr_metrics_vietnamese_thousands_separator` |
| Bộ chấm: “km²”/“km2” bị đếm là con số | PhoWhisper ghi “km vuông” bị tính mất số | NFKC + chỉ token toàn chữ số mới là số | cùng test |
| So sánh không công bằng giữa model ghi số bằng chữ và bằng chữ số | PhoWhisper “giữ số” = 0 % | Thêm cột `*_digits`; script lưu toàn bộ bản chép, `--reuse` chấm lại không cần chép lại | — |

## 5. End-to-end tiếng Việt (`scripts/e2e_lecture_vi.py`) — 12/12

Supabase + worker GPU + OpenAI thật; user tạm, xóa sạch sau khi chạy.

| Bước | Kết quả |
|---|---|
| Xử lý bản sạch / nhiễu | `ready`, 108 s / 50 s (lần đầu có nạp model) |
| Bản chép của pipeline | WER 5,8 % / 6,5 %, giữ số 100 %, lệch đầu câu p50 188 ms, max 1,5 s, 8/8 câu có segment |
| OCR bảng (EasyOCR vi+en) | đọc đúng dòng viết thêm “Chưa chứng minh tốt hơn rerank toàn bài” |
| “Hằng số k … bằng bao nhiêu?” | ✓ “… bằng 60” (từ bảng). Claim từ lời giảng **bị loại** (`below_support_threshold`) vì ASR chép “ca” thay “k” — đúng hành vi: evidence sai thì không xác nhận |
| “Lập chỉ mục 70 bài báo mất bao lâu …?” | ✓ 32 giây; ✓ khoảng 3.097 giây — **bằng tiếng Việt**, có locator ms, nhãn `automatic_transcript` |
| “Có tốt hơn RAPTOR trên PeerQA không?” | `insufficient_evidence` — không overclaim “tốt hơn”. Lý tưởng là trả lời “không kém, chưa chứng minh tốt hơn”: bảo thủ, mất recall |
| “Được công bố năm nào?” (không có trong bài) | `insufficient_evidence` — không bịa |

Hồi quy: `scripts/e2e_lecture.py` (bài giảng tiếng Anh) 35/35 với part mặc định 45 MB (ca “complete khi thiếu
phần → 409” chỉ chạy khi `MSKS_MEDIA_PART_BYTES` nhỏ); `pytest -q` 72/72.

## 6. NLI tiếng Việt (`tests/fixtures/nli_vi_lecture.jsonl`, 40 cặp tự gán nhãn)

Quyết định “được hỗ trợ” = entail ≥ s và contradiction < 0,5 (như `edahr.verification`).

| Cấu hình | Precision | Recall | Sai “được hỗ trợ” | Bỏ sót |
|---|---|---|---|---|
| `mDeBERTa-v3-base-mnli-xnli` | 0,882 | 0,833 | c06 (“hai trăm hai mươi” ↔ 2200), x03 (“không kém” → “tốt hơn”) | n02, a01, a04 |
| + `words_to_digits` + `claim_guard` (đang dùng) | **1,0** | **0,889** | — | n02, a01 |
| `DeBERTa-v3-base-mnli-fever-anli` (EN, profile product) | 0,929 | 0,722 | g04 (phủ định “giảm” ↔ “tăng”) | p07, n03, n04, a01, a04 |

- mDeBERTa cho điểm gần như chỉ 0 hoặc 1: kết quả **không đổi** từ s = 0,25 đến 0,9 → chỉnh ngưỡng không sửa được lỗi.
- **Cảnh báo thiên lệch:** `claim_guard` và chuẩn hóa số được viết *sau khi* thấy c06/x03/a04 trên chính bộ này.
  Precision 1,0 là trên dữ liệu đã nhìn; chưa phải số đo độc lập.

## 7. Giới hạn của phép đo

- FLEURS dev: **chỉ giọng nam**, câu đọc từ Wikipedia — không có ngập ngừng, nói nhanh, tiếng vang phòng học.
- Bài giảng mẫu là **TTS** 53 s — cận trên chất lượng; không thay được bài giảng thật dài 60 phút.
- 40 cặp NLI quá nhỏ để hiệu chỉnh; người viết chốt cũng là người gán nhãn.
- RTF/VRAM đo trên GPU 6 GB có thể khác máy đích (ví dụ RTX 3090).
- Chưa đo: giọng Nam/Trung thật, nhiều người nói, OCR công thức, bài dài > 10 phút qua pipeline.

## 8. Việc nên làm tiếp (theo thứ tự)

1. **Bộ held-out NLI độc lập** (≥ 200 cặp, người khác gán nhãn, lấy từ bản chép bài giảng thật) để đo lại
   precision của chốt và hiệu chỉnh ngưỡng `lecture_vi_v1`.
2. **2–3 bài giảng thật có transcript chuẩn** (có giọng Nam, có thuật ngữ Anh) → chạy lại mục 3 + e2e.
3. ~~Glossary theo workspace~~ — **đã làm 10/10**: `PUT /api/workspaces/{id}/asr-glossary` (≤ 40 thuật ngữ, 400 ký tự).
   Qua pipeline thật: WER 5,8 % → 3,62 % (sạch), 6,5 % → 4,35 % (nhiễu); `e2e_lecture_vi.py --glossary` 16/16.
   Còn thiếu: thao tác chép lại bài đã xử lý với glossary mới; đo trên bài thật (glossary sai có thể làm Whisper
   chèn thuật ngữ vào đoạn im lặng).
4. Chốt so sánh còn hẹp (danh sách cụm từ cố định). “rưỡi” đã được đổi khi đứng sau trăm/nghìn/triệu/tỷ
   (“hai trăm rưỡi” = 250); “hai giờ rưỡi”, “một tiếng rưỡi” cố ý giữ nguyên.
5. Nhãn `support_basis = automatic_transcript` đang dùng chung cho OCR bảng; cân nhắc tách `automatic_ocr` cho UI.

## 9. Chạy lại

```bash
# ASR (cần data/eval/fleurs_vi; model PhoWhisper tải vào data/models bằng local_dir)
python scripts/eval_asr_vi.py --limit 120 --out analysis/asr_vi_fleurs_dev120 \
  --models ct2:mobiuslabsgmbh/faster-whisper-large-v3-turbo ct2:data/models/PhoWhisper-large-ct2
python scripts/eval_asr_vi.py --limit 120 --out analysis/asr_vi_fleurs_dev120 --reuse analysis/asr_vi_fleurs_dev120.json  # chỉ chấm lại

# PhoWhisper → CTranslate2
python -c "from ctranslate2.converters import TransformersConverter as T; T('data/models/PhoWhisper-large', copy_files=['tokenizer.json','preprocessor_config.json']).convert('data/models/PhoWhisper-large-ct2', quantization='float16')"

# NLI
python scripts/eval_nli_vi.py

# Bài giảng mẫu + end-to-end (API + worker đang chạy cùng config)
python scripts/make_sample_lecture_vi.py OUT
python scripts/e2e_lecture_vi.py OUT
```

| File | Nội dung |
|---|---|
| `asr_vi_fleurs_dev120.json` | 5 cấu hình × 120 câu, kèm toàn bộ bản chép |
| `asr_vi_fleurs_dev_ct2.json` | 361 câu dev, turbo + medium (chuẩn hóa NFC cũ) |
| `lecture_vi_glossary.json` | bài giảng mẫu: turbo/PhoWhisper × có/không glossary × sạch/nhiễu |
| `lecture_vi_e2e.json` | e2e: chất lượng bản chép pipeline + câu trả lời QA |
| `lecture_vi_e2e_glossary.json` | e2e với glossary workspace 7 thuật ngữ |
| `nli_vi_lecture.json` | NLI 40 cặp theo ngưỡng và loại câu |
