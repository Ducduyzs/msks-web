# Snapshot EDAHR

`src/edahr/` là bản sao **không sửa** của thư viện EDAHR (ARCHITECTURE.md mục 2.3, 9).

| | |
|---|---|
| Repo gốc | `D:\AI PROJECT\Evidence-Density-Aware Adaptive Hierarchical Retrieval for Scientific Document Question Answering` |
| Commit | `b28e650d49865fbb9c9c19f350f0a2670061e728` (cấu hình thí nghiệm v11) |
| Checksum | [EDAHR_CHECKSUMS.sha256](EDAHR_CHECKSUMS.sha256) — `cd src/edahr && sha256sum -c ../EDAHR_CHECKSUMS.sha256` |

MSKS chỉ import các module nhẹ: `schemas`, `config`, `text`, `hierarchy`, `ingestion`, `agreement`,
`context`, `experimental_v9`, `verification`, `models`. Mọi thay đổi hành vi nằm trong `src/msks`.
