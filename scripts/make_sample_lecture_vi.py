"""Tạo bài giảng tiếng Việt mẫu (giọng neural vi-VN) để kiểm thử ASR + pipeline + QA tiếng Việt.

    python scripts/make_sample_lecture_vi.py OUT_DIR

Kịch bản do dự án tự viết (không phải dữ liệu người dùng), gửi tới dịch vụ TTS của Microsoft qua `edge-tts`.
Có thuật ngữ tiếng Anh xen kẽ và số liệu — đúng loại lỗi ASR cần đo. Xuất:
  sample_lecture_vi.mp4         bản sạch
  sample_lecture_vi_noisy.mp4   cùng nội dung + nhiễu hồng SNR ≈ 15 dB
  sample_lecture_vi.srt         phụ đề đúng lời đọc (thử nhánh caption)
  sample_lecture_vi.json        câu tham chiếu + mốc thời gian (tính WER, sai số timestamp)
Giọng đọc tổng hợp sạch hơn giảng viên thật: kết quả ở đây là cận trên, không thay đánh giá trên bài giảng thật.
"""
from __future__ import annotations

import json
import time
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VOICES = ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"]
SLIDES = [
    {
        "lines": ["Agreement Ranking", "RRF giữa cross-encoder và SBERT", "Hằng số k = 60"],
        "sentences": [
            "Hôm nay chúng ta học về Agreement Ranking, một phương pháp xếp hạng đoạn văn cho hệ thống hỏi đáp.",
            "Phương pháp này kết hợp thứ hạng của cross-encoder với thứ hạng của SBERT bằng reciprocal rank fusion.",
            "Hằng số k trong công thức được đặt bằng 60.",
        ],
    },
    {
        "lines": ["Chi phí lập chỉ mục", "Không dựng cây tóm tắt", "70 bài: 32 giây so với 3097 giây"],
        "sentences": [
            "Khác với RAPTOR, Agreement Ranking không cần dựng cây tóm tắt.",
            "Với 70 bài báo, thời gian lập chỉ mục chỉ mất 32 giây, trong khi RAPTOR mất khoảng 3097 giây.",
        ],
    },
    {
        "lines": ["Kết quả trên PeerQA", "Không kém RAPTOR"],
        "sentences": [
            "Trên tập dữ liệu PeerQA, Agreement Ranking không kém RAPTOR về độ chính xác của bằng chứng.",
        ],
    },
    {
        "lines": ["Kết quả trên PeerQA", "Không kém RAPTOR", "Chưa chứng minh tốt hơn rerank toàn bài"],
        "sentences": [
            "Tuy nhiên, phương pháp này chưa được chứng minh là tốt hơn việc xếp hạng lại toàn bộ bài báo.",
            "Với bài giảng tiếng Việt, chúng ta cần đánh giá lại mô hình kiểm chứng trước khi tin vào kết quả.",
        ],
    },
]
GAP = 0.6


def run(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, stdin=subprocess.DEVNULL, timeout=600)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def tts(text: str, voice: str, mp3: Path) -> None:
    """Gọi edge-tts như tiến trình con có timeout + thử lại: kết nối dịch vụ đôi khi treo không báo lỗi."""
    if mp3.exists() and mp3.stat().st_size > 1000:
        return  # đã tạo ở lần chạy trước
    for attempt in range(6):
        if attempt:
            time.sleep(5 * attempt)
        try:
            subprocess.run([sys.executable, "-m", "edge_tts", "--voice", voice, "--text", text, "--write-media", str(mp3)],
                           check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            if mp3.exists() and mp3.stat().st_size > 1000:
                return
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass
        print(f"  thử lại TTS ({attempt + 1}) …", flush=True)
    raise RuntimeError(f"TTS thất bại: {text[:40]}")


def slide_image(lines: list[str], path: Path) -> None:
    title = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 56)
    body = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 44)
    image = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 80), lines[0], fill="black", font=title)
    for i, line in enumerate(lines[1:]):
        draw.text((100, 230 + i * 110), "• " + line, fill="black", font=body)
    image.save(path, quality=92)


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_lecture_vi")
    work = out / "work_vi"
    work.mkdir(parents=True, exist_ok=True)
    cues, wavs, concat = [], [], []
    t = 0.5
    for s_index, slide in enumerate(SLIDES):
        voice = VOICES[s_index % len(VOICES)]
        png = work / f"slide_{s_index}.png"
        slide_image(slide["lines"], png)
        slide_start = t
        for n, sentence in enumerate(slide["sentences"]):
            mp3 = work / f"s{s_index}_{n}.mp3"
            wav = work / f"s{s_index}_{n}.wav"
            tts(sentence, voice, mp3)
            run("-i", str(mp3), "-ar", "22050", "-ac", "1", str(wav))
            length = duration(wav)
            cues.append({"start": round(t, 3), "end": round(t + length, 3), "text": sentence, "voice": voice})
            wavs.append(wav)
            t += length + GAP
        concat.append(f"file '{png.resolve().as_posix()}'\nduration {t - slide_start:.3f}")
    concat.append(f"file '{(work / f'slide_{len(SLIDES) - 1}.png').resolve().as_posix()}'")
    (work / "slides.txt").write_text("\n".join(concat), encoding="utf-8")

    inputs = ["-f", "lavfi", "-t", "0.5", "-i", "anullsrc=r=22050:cl=mono"]
    labels = ["[0:a]"]
    for i, wav in enumerate(wavs, start=1):
        inputs += ["-i", str(wav), "-f", "lavfi", "-t", str(GAP), "-i", "anullsrc=r=22050:cl=mono"]
        labels += [f"[{2 * i - 1}:a]", f"[{2 * i}:a]"]
    speech = work / "speech.wav"
    run(*inputs, "-filter_complex", "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[a]", "-map", "[a]", str(speech))
    # Nhiễu hồng ~ -15 dB so với lời nói (âm lượng giọng TTS ≈ -16 dBFS).
    noisy = work / "speech_noisy.wav"
    run("-i", str(speech), "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.06:r=22050:d={t + 1:.1f}",
        "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]", "-map", "[a]", str(noisy))
    for audio, name in ((speech, "sample_lecture_vi.mp4"), (noisy, "sample_lecture_vi_noisy.mp4")):
        run("-f", "concat", "-safe", "0", "-i", str(work / "slides.txt"), "-i", str(audio),
            "-vf", "fps=10,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k",
            "-shortest", str(out / name))
    (out / "sample_lecture_vi.srt").write_text(
        "\n".join(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n" for i, c in enumerate(cues, 1)),
        encoding="utf-8")
    (out / "sample_lecture_vi.json").write_text(json.dumps({"cues": cues, "voices": VOICES}, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    print(f"{out / 'sample_lecture_vi.mp4'}: {duration(out / 'sample_lecture_vi.mp4'):.1f} s, {len(cues)} câu")


if __name__ == "__main__":
    main()
