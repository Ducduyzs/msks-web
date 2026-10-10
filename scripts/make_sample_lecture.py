"""Tạo video bài giảng mẫu để kiểm thử pipeline media (không dùng dữ liệu thật).

    python scripts/make_sample_lecture.py OUT_DIR

Slide có chữ tiếng Việt + số liệu; slide cuối được "viết dần" (thêm dòng) để thử chọn frame khi bảng
thay đổi tăng dần. Lời giảng tổng hợp bằng giọng TTS tiếng Anh của Windows (System.Speech) — máy dev
không có giọng tiếng Việt, nên phần ASR tiếng Việt cần thử thêm bằng bài giảng thật.
Tạo kèm `sample_lecture.srt` (đúng lời đọc) để thử nhánh caption/YouTube.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SLIDES = [
    {
        "lines": ["Agreement Ranking", "RRF giữa cross-encoder và SBERT", "Hằng số k = 60"],
        "speech": "Today we study Agreement Ranking. It fuses the cross encoder ranking with the sentence embedding "
                  "ranking using reciprocal rank fusion, with the constant k equal to sixty.",
    },
    {
        "lines": ["Chi phí lập chỉ mục", "Không dựng cây tóm tắt", "70 bài: 32 s so với 3097 s"],
        "speech": "Agreement Ranking does not build a summary tree. Building the index for seventy papers took "
                  "thirty two seconds, while RAPTOR needed about three thousand seconds.",
    },
    {
        "lines": ["Kết quả trên PeerQA", "Không kém RAPTOR"],
        "speech": "On the PeerQA dataset, Agreement Ranking was not worse than RAPTOR.",
    },
    {
        "lines": ["Kết quả trên PeerQA", "Không kém RAPTOR", "Chưa chứng minh tốt hơn rerank toàn bài"],
        "speech": "However, it was not shown to be better than reranking every passage of the paper.",
    },
]


def tts(text: str, wav: Path) -> None:
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate = 0; "
        f"$s.SetOutputToWaveFile('{wav}'); $s.Speak([Console]::In.ReadToEnd()); $s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], input=text.encode("utf-8"), check=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def slide(lines: list[str], path: Path) -> None:
    font_title = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 56)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 44)
    image = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 80), lines[0], fill="black", font=font_title)
    for i, line in enumerate(lines[1:]):
        draw.text((100, 230 + i * 110), "• " + line, fill="black", font=font)
    image.save(path, quality=92)


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_lecture")
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    work.mkdir(exist_ok=True)
    concat, audio, cues, t = [], [], [], 0.5
    for i, item in enumerate(SLIDES):
        wav = work / f"speech_{i}.wav"
        png = work / f"slide_{i}.png"
        tts(item["speech"], wav.resolve())
        slide(item["lines"], png)
        length = duration(wav) + 0.6
        concat.append(f"file '{png.resolve().as_posix()}'\nduration {length:.3f}")
        audio.append(wav)
        cues.append((t, t + length - 0.6, item["speech"]))
        t += length
    concat.append(f"file '{(work / f'slide_{len(SLIDES) - 1}.png').resolve().as_posix()}'")
    (work / "slides.txt").write_text("\n".join(concat), encoding="utf-8")
    # Âm thanh: 0,5 s im lặng đầu + các câu cách nhau 0,6 s.
    parts = ["-f", "lavfi", "-t", "0.5", "-i", "anullsrc=r=22050:cl=mono"]
    filters = ["[0:a]"]
    for index, wav in enumerate(audio, start=1):
        parts += ["-i", str(wav)]
        parts += ["-f", "lavfi", "-t", "0.6", "-i", "anullsrc=r=22050:cl=mono"]
        filters += [f"[{2 * index - 1}:a]", f"[{2 * index}:a]"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *parts, "-filter_complex",
                    "".join(filters) + f"concat=n={len(filters)}:v=0:a=1[a]", "-map", "[a]", str(work / "speech.wav")],
                   check=True)
    video = out / "sample_lecture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "slides.txt"),
                    "-i", str(work / "speech.wav"), "-vf", "fps=10,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-c:a", "aac", "-b:a", "96k", "-shortest", str(video)], check=True)
    (out / "sample_lecture.srt").write_text(
        "\n".join(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n" for n, (a, b, text) in enumerate(cues, 1)),
        encoding="utf-8")
    shutil.rmtree(work)
    print(f"{video} ({video.stat().st_size // 1024} KB, {duration(video):.1f} s) + sample_lecture.srt")


if __name__ == "__main__":
    main()
