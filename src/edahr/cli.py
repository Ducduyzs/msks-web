from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Settings
from .runtime import build_pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_ROOT = PROJECT_ROOT / "data" / "raw_pdfs"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Neural EDAHR scientific-document QA")
    root.add_argument("pdf", nargs="+", help="PDF files or a directory containing PDFs")
    root.add_argument("--question", "-q", required=True)
    root.add_argument("--config", help="JSON settings file; defaults to config.local.json")
    return root


def _config_path(raw: str | None) -> Path | None:
    if raw is None:
        default = PROJECT_ROOT / "config.local.json"
        return default if default.is_file() else None
    supplied = Path(raw).expanduser()
    candidates = (
        supplied,
        PROJECT_ROOT / supplied,
        PROJECT_ROOT / supplied.name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Không tìm thấy config: {raw}. Config mặc định phải nằm tại: "
        f"{PROJECT_ROOT / 'config.local.json'}"
    )


def _pdf_paths(values: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw in values:
        supplied = Path(raw).expanduser()
        candidates = (
            supplied,
            PROJECT_ROOT / supplied,
            PDF_ROOT / supplied.name,
        )
        found = next((candidate.resolve() for candidate in candidates if candidate.exists()), None)
        if found is None:
            available = sorted(path.name for path in PDF_ROOT.glob("*.pdf")) if PDF_ROOT.exists() else []
            listing = ", ".join(available) if available else "(thư mục đang trống)"
            raise FileNotFoundError(
                f"Không tìm thấy PDF: {raw}. Các PDF hiện có trong {PDF_ROOT}: {listing}"
            )
        if found.is_dir():
            resolved.extend(sorted(found.glob("*.pdf")))
        elif found.suffix.casefold() == ".pdf":
            resolved.append(found)
        else:
            raise ValueError(f"Đầu vào không phải PDF: {found}")
    if not resolved:
        raise FileNotFoundError("Không tìm thấy PDF nào để xử lý.")
    return resolved


def main() -> None:
    args = parser().parse_args()
    try:
        config_path = _config_path(args.config)
        pdf_paths = _pdf_paths(args.pdf)
    except (FileNotFoundError, ValueError) as exc:
        parser().error(str(exc))
    settings = Settings.from_json(config_path) if config_path else Settings()
    result = build_pipeline(pdf_paths, settings).answer(args.question)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
