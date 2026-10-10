"""So sánh model ASR trên giọng tiếng Việt có bản chép chuẩn (FLEURS vi_vn, CC BY 4.0).

    python scripts/eval_asr_vi.py --fleurs data/eval/fleurs_vi --split dev --out analysis/asr_vi_fleurs_dev

Model mặc định:
  ct2:mobiuslabsgmbh/faster-whisper-large-v3-turbo   (mặc định của pipeline)
  ct2:Systran/faster-whisper-medium
  hf:vinai/PhoWhisper-medium
  hf:vinai/PhoWhisper-large

Đo WER/CER (đã chuẩn hóa NFC, bỏ dấu câu), tỉ lệ giữ đúng con số, RTF (giây xử lý / giây audio) và VRAM đỉnh
(nvidia-smi, trừ mức nền). Dùng `dev` để chọn model; `test` để báo cáo cuối — không chọn trên test.
Nên tắt worker trong lúc đo để VRAM/tốc độ không bị tranh chấp.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from msks.eval_asr import Score, normalize_vi, score  # noqa: E402
from msks.vi_text import words_to_digits  # noqa: E402

DEFAULT_MODELS = [
    "ct2:mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "ct2:Systran/faster-whisper-medium",
    "hf:data/models/PhoWhisper-medium",   # vinai/PhoWhisper-medium (BSD-3), tải bằng local_dir
    "hf:data/models/PhoWhisper-large",
]


class VramPeak:
    """Đỉnh bộ nhớ GPU (MiB) trong khối lệnh, trừ mức nền lúc bắt đầu."""

    def __init__(self) -> None:
        self.peak = 0
        self.stop = threading.Event()

    @staticmethod
    def used() -> int:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout
        return int(out.strip().splitlines()[0])

    def _poll(self) -> None:
        while not self.stop.wait(0.25):
            self.peak = max(self.peak, self.used())

    def __enter__(self) -> "VramPeak":
        self.base = self.used()
        self.thread = threading.Thread(target=self._poll, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join()
        self.delta = max(0, self.peak - self.base)


def load_fleurs(folder: Path, split: str, limit: int | None) -> list[dict]:
    rows = []
    with open(folder / f"{split}.tsv", encoding="utf-8") as handle:
        for line in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
            sentence_id, filename, raw, _normalized, _chars, samples, gender = line[:7]
            path = folder / split / filename
            if path.exists():
                rows.append({"id": f"{sentence_id}/{filename}", "path": path, "reference": raw,
                             "seconds": int(samples) / 16000, "gender": gender})
    return rows[:limit] if limit else rows


def transcribe_ct2(name: str, rows: list[dict], glossary: str | None) -> list[str]:
    from msks.media.asr import load_ctranslate2_cuda

    load_ctranslate2_cuda()
    from faster_whisper import WhisperModel

    model = WhisperModel(name, device="cuda", compute_type="int8_float16")
    out = []
    for row in rows:
        segments, _ = model.transcribe(str(row["path"]), language="vi", beam_size=5, vad_filter=False,
                                       condition_on_previous_text=False, initial_prompt=glossary)
        out.append(" ".join(s.text.strip() for s in segments))
    del model
    return out


def transcribe_hf(name: str, rows: list[dict], glossary: str | None) -> list[str]:
    import torch
    from transformers import pipeline

    # Câu > 30 s: chia đoạn 30 s thay vì ép sinh timestamp (PhoWhisper không chắc được huấn luyện với timestamp).
    asr = pipeline("automatic-speech-recognition", model=name, dtype=torch.float16, device=0, chunk_length_s=30)
    out = []
    for row in rows:
        audio, rate = sf.read(str(row["path"]), dtype="float32")
        result = asr({"raw": audio, "sampling_rate": rate},
                     generate_kwargs={"language": "vietnamese", "task": "transcribe", "num_beams": 5})
        out.append(result["text"].strip())
    del asr
    torch.cuda.empty_cache()
    return out


def evaluate(spec: str, rows: list[dict], glossary: str | None, previous: dict | None = None) -> dict:
    backend, name = spec.split(":", 1)
    audio_seconds = sum(r["seconds"] for r in rows)
    if previous:  # --reuse: chấm lại bản chép đã lưu, giữ thời gian/VRAM của lần chạy đó
        by_id = {h["id"]: h["hypothesis"] for h in previous["hypotheses"]}
        hyps = [by_id[r["id"]] for r in rows]
        elapsed, vram_delta = previous["seconds"], previous["vram_peak_mib"]
    else:
        with VramPeak() as vram:
            t0 = time.perf_counter()
            hyps = (transcribe_ct2 if backend == "ct2" else transcribe_hf)(name, rows, glossary)
            elapsed = time.perf_counter() - t0
        vram_delta = vram.delta
    scores = [score(r["reference"], h) for r, h in zip(rows, hyps)]
    total = sum(scores, Score(0, 0, 0, 0, 0, 0))
    # Cùng phép đo sau khi đổi số viết bằng chữ → chữ số ở cả hai phía: PhoWhisper chép "hai mươi", FLEURS ghi "20".
    digits = sum((score(words_to_digits(r["reference"]), words_to_digits(h)) for r, h in zip(rows, hyps)),
                 Score(0, 0, 0, 0, 0, 0))
    by_gender = {}
    for gender in sorted({r["gender"] for r in rows}):
        part = [s for s, r in zip(scores, rows) if r["gender"] == gender]
        by_gender[gender] = round(sum(part, Score(0, 0, 0, 0, 0, 0)).wer, 4)
    per_utt = sorted(
        ({"id": r["id"], "wer": round(s.wer, 3), "reference": normalize_vi(r["reference"]), "hypothesis": normalize_vi(h)}
         for r, h, s in zip(rows, hyps, scores)),
        key=lambda x: -x["wer"],
    )
    return {
        "model": spec, "utterances": len(rows), "audio_minutes": round(audio_seconds / 60, 1),
        "wer": round(total.wer, 4), "cer": round(total.cer, 4),
        "number_recall": round(total.number_recall, 4) if total.number_recall is not None else None,
        "numbers_in_reference": total.numbers_ref,
        "median_utt_wer": round(statistics.median(s.wer for s in scores), 4),
        "wer_digits": round(digits.wer, 4), "cer_digits": round(digits.cer, 4),
        "number_recall_digits": round(digits.number_recall, 4) if digits.number_recall is not None else None,
        "wer_by_gender": by_gender,
        "rtf": round(elapsed / audio_seconds, 4), "seconds": round(elapsed, 1), "vram_peak_mib": vram_delta,
        "worst": per_utt[:5],
        "hypotheses": [{"id": r["id"], "hypothesis": h} for r, h in zip(rows, hyps)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleurs", type=Path, default=ROOT / "data/eval/fleurs_vi")
    parser.add_argument("--split", default="dev")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--glossary")
    parser.add_argument("--out", type=Path, default=ROOT / "analysis/asr_vi_fleurs_dev")
    parser.add_argument("--reuse", type=Path, help="file kết quả trước (có 'hypotheses'): chấm lại, không chép lại")
    args = parser.parse_args()
    rows = load_fleurs(args.fleurs, args.split, args.limit)
    previous = {}
    if args.reuse and args.reuse.exists():
        previous = {r["model"]: r for r in json.loads(args.reuse.read_text(encoding="utf-8"))["results"] if "hypotheses" in r}
    print(f"{len(rows)} câu, {sum(r['seconds'] for r in rows) / 60:.1f} phút audio", flush=True)
    results = []
    for spec in args.models:
        print(f"== {spec}", flush=True)
        result = evaluate(spec, rows, args.glossary, previous.get(spec))
        print({k: v for k, v in result.items() if k not in ("worst", "hypotheses")}, flush=True)
        results.append(result)
        # ghi sau mỗi model: dừng giữa chừng vẫn giữ được bản chép của các model đã xong (dùng với --reuse)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".partial.json").write_text(json.dumps({"results": results}, ensure_ascii=False),
                                                         encoding="utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    meta = {"dataset": f"google/fleurs vi_vn {args.split}", "license": "CC BY 4.0", "utterances": len(rows),
            "normalization": "NFC, lowercase, punctuation/symbols removed except %, digits kept as written; "
                             "*_digits: number words → digits (msks.vi_text.words_to_digits) on both sides first",
            "decoding": "language=vi, beam=5, no VAD, condition_on_previous_text=False", "glossary": args.glossary}
    args.out.with_suffix(".json").write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    print(f"→ {args.out.with_suffix('.json')}")


if __name__ == "__main__":
    main()
