"""Kiểm tra NLI trên câu bài giảng tiếng Việt (LECTURE_ARCHITECTURE.md mục 7 — lớp "hỗ trợ claim").

    python scripts/eval_nli_vi.py [--out analysis/nli_vi_lecture]

Bộ `tests/fixtures/nli_vi_lecture.jsonl` (40 cặp tự gán nhãn: diễn đạt lại, số khớp/sai, phủ định, thêm thông
tin, không liên quan, bản chép có lỗi ASR) **chỉ là kiểm tra khói**: quá nhỏ để hiệu chỉnh ngưỡng. Báo cáo
quyết định "được hỗ trợ" (support ≥ s và contradiction < c, đúng như edahr.verification) ở nhiều ngưỡng để
thấy ngưỡng v11 0,25/0,50 có hợp với tiếng Việt không.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODELS = {
    "lecture_vi_v1": ("MoritzLaurer/mDeBERTa-v3-base-mnli-xnli", False),
    "lecture_vi_v1 + chuẩn hóa số + chốt": ("MoritzLaurer/mDeBERTa-v3-base-mnli-xnli", True),
    "product (EN)": ("MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli", False),
}
SUPPORT_GRID = [0.25, 0.5, 0.7, 0.8, 0.9]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "tests/fixtures/nli_vi_lecture.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "analysis/nli_vi_lecture")
    parser.add_argument("--contradiction", type=float, default=0.5)
    args = parser.parse_args()
    from edahr.models import NliVerifier
    from msks.vi_text import claim_guard, words_to_digits

    pairs = [json.loads(line) for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = {"meta": {"pairs": len(pairs), "contradiction_threshold": args.contradiction,
                       "decision": "supported = entail >= s and contradiction < c"}, "models": {}}
    cache: dict = {}
    for profile, (model_name, guarded) in MODELS.items():
        verifier = cache.setdefault(model_name, NliVerifier(model_name, "cuda"))
        rows = []
        for pair in pairs:
            hypothesis, premise = pair["hypothesis"], pair["premise"]
            if guarded:
                hypothesis, premise = words_to_digits(hypothesis), words_to_digits(premise)
            entail, contradiction = verifier.score_details(hypothesis, premise)
            guard = claim_guard(pair["hypothesis"], pair["premise"]) if guarded else None
            if guard:
                entail = 0.0  # chốt sau NLI: không được publish
            rows.append({**pair, "entail": round(entail, 4), "contradiction": round(contradiction, 4), "guard": guard})
        by_threshold = {}
        for s in SUPPORT_GRID:
            tp = sum(1 for r in rows if r["label"] == "entailment" and r["entail"] >= s and r["contradiction"] < args.contradiction)
            fp = sum(1 for r in rows if r["label"] != "entailment" and r["entail"] >= s and r["contradiction"] < args.contradiction)
            positives = sum(1 for r in rows if r["label"] == "entailment")
            by_threshold[str(s)] = {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                                    "recall": round(tp / positives, 3), "false_supported": fp,
                                    "false_ids": [r["id"] for r in rows if r["label"] != "entailment" and r["entail"] >= s
                                                  and r["contradiction"] < args.contradiction]}
        categories = defaultdict(list)
        for r in rows:
            categories[r["category"]].append(r)
        per_category = {
            c: {"n": len(rs), "label": rs[0]["label"], "mean_entail": round(sum(r["entail"] for r in rs) / len(rs), 3),
                "mean_contradiction": round(sum(r["contradiction"] for r in rs) / len(rs), 3),
                "supported_at_0.25": sum(1 for r in rs if r["entail"] >= 0.25 and r["contradiction"] < args.contradiction)}
            for c, rs in categories.items()
        }
        report["models"][profile] = {"model": model_name, "by_support_threshold": by_threshold,
                                     "per_category": per_category, "rows": rows}
        print(f"== {profile} ({model_name})")
        for s, m in by_threshold.items():
            print(f"   s={s}: precision={m['precision']} recall={m['recall']} sai={m['false_ids']}")
        for c, m in per_category.items():
            print(f"   {c:16s} n={m['n']} nhãn={m['label']:13s} entail={m['mean_entail']:.2f} "
                  f"contra={m['mean_contradiction']:.2f} hỗ_trợ@0.25={m['supported_at_0.25']}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {args.out.with_suffix('.json')}")


if __name__ == "__main__":
    main()
