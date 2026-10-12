"""Dịch danh mục câu giao diện (web/src/i18n/catalog.json) sang các ngôn ngữ khác bằng LLM đã cấu hình.

    cd web && npm run i18n:extract          # cập nhật danh mục từ mã nguồn
    python scripts/translate_ui.py [--lang en zh ja ...] [--force]

Chỉ dịch câu còn thiếu: bản dịch đã có (kể cả người sửa tay) được giữ; câu không còn trong danh mục bị bỏ. Mỗi câu
được kiểm tra: giữ nguyên biến {0}/{tên}, khoảng trắng đầu/cuối; sai thì dịch lại riêng, vẫn sai thì để trống
(giao diện hiện tiếng Việt). Bản dịch do máy tạo — nên có người bản ngữ rà lại.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from msks.settings import get_settings  # noqa: E402

I18N = ROOT / "web/src/i18n"
LANGUAGES = {
    "en": "English (US)",
    "zh": "Simplified Chinese (简体中文)",
    "ja": "Japanese (日本語), polite UI style",
    "ko": "Korean (한국어), polite UI style",
    "fr": "French (Français)",
    "de": "German (Deutsch)",
    "es": "Spanish (Español)",
}
BATCH = 60
PLACEHOLDER = re.compile(r"\{\w+\}")

SYSTEM = """You translate user-interface strings of MSKS, a web app that synthesizes knowledge from many sources with
verification: users upload documents and lecture videos, ask questions, and get claims with citations checked by an NLI
verifier. Translate from Vietnamese into {language}.
Rules:
- Return exactly one translation per input item, same index.
- Keep placeholders such as {{0}}, {{1}}, {{name}} exactly, in a natural position.
- Do not add or remove leading/trailing spaces; keep separators like "·", "—", "…", "/" and trailing punctuation.
- Keep product and technical names unchanged: MSKS, PDF, DOI, arXiv, YouTube, Google Drive, Supabase, OpenAI, LLM, NLI,
  OCR, ASR, TOTP, CSRF, JSON, Markdown, SRT, VTT, MP4, WebM, RAPTOR, Agreement Ranking, SBERT, BGE-M3, SSE, token.
- "workspace" is a container of sources; translate it the way that language's software usually does.
- Keep the capitalization pattern: a Vietnamese string starting with an uppercase letter is a heading, button, label or
  sentence → start with uppercase; one starting lowercase is a fragment joined to other text → keep it lowercase.
- Each item has "where": the source files using it (e.g. pages/SecurityPage.tsx, lib/labels.ts). Use it for context;
  page titles and navigation/list labels usually take plural nouns ("Workspaces", "Sources", "Terms …").
- Items may include "en": a human-reviewed English translation. Use it to resolve the intended meaning (e.g. "Đưa lên"
  means move an item up, not upload), but write natural target-language UI text rather than translating the English.
- Short, neutral UI wording. No explanations, no quotes around the output."""


def schema(n: int) -> dict:
    return {
        "name": "ui_translations",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {"items": {"type": "array", "minItems": n, "maxItems": n, "items": {
                "type": "object", "additionalProperties": False, "required": ["i", "text"],
                "properties": {"i": {"type": "integer"}, "text": {"type": "string"}}}}},
        },
    }


def valid(source: str, target: str) -> bool:
    return bool(target.strip()) and sorted(PLACEHOLDER.findall(source)) == sorted(PLACEHOLDER.findall(target))


def keep_spacing(source: str, target: str) -> str:
    lead = source[: len(source) - len(source.lstrip())]
    trail = source[len(source.rstrip()):]
    return lead + target.strip() + trail


def translate(client, model: str, language: str, items: list[str], where: dict, english: dict | None = None) -> list[str | None]:
    payload = [{"i": i, "text": text, "where": ", ".join(where.get(text) or []),
                **({"en": english[text]} if english and text in english else {})} for i, text in enumerate(items)]
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_schema", "json_schema": schema(len(items))},
        messages=[{"role": "system", "content": SYSTEM.format(language=language)},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    out: list[str | None] = [None] * len(items)
    for item in json.loads(response.choices[0].message.content)["items"]:
        i = item["i"]
        if 0 <= i < len(items) and valid(items[i], item["text"]):
            out[i] = keep_spacing(items[i], item["text"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", nargs="+", default=list(LANGUAGES))
    parser.add_argument("--force", action="store_true", help="dịch lại tất cả, bỏ bản dịch cũ")
    args = parser.parse_args()
    import openai

    settings = get_settings()
    client = openai.OpenAI(api_key=settings.openai_api_key, timeout=120, max_retries=2)
    data = json.loads((I18N / "catalog.json").read_text(encoding="utf-8"))
    catalog, where = data["messages"], data.get("where", {})
    english_path = I18N / "locales" / "en.json"
    for code in args.lang:
        # Tiếng Anh đã được người rà → làm tham chiếu nghĩa cho các ngôn ngữ khác.
        english = json.loads(english_path.read_text(encoding="utf-8")) if code != "en" and english_path.exists() else None
        path = I18N / "locales" / f"{code}.json"
        existing = {} if args.force or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
        result = {m: existing[m] for m in catalog if m in existing}
        todo = [m for m in catalog if m not in result]
        failed: list[str] = []
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            for source, target in zip(batch, translate(client, settings.llm_model, LANGUAGES[code], batch, where, english)):
                if target is None:
                    retry = translate(client, settings.llm_model, LANGUAGES[code], [source], where, english)[0]
                    if retry is None:
                        failed.append(source)
                        continue
                    target = retry
                result[source] = target
            print(f"  {code}: {min(start + BATCH, len(todo))}/{len(todo)}", flush=True)
        ordered = {m: result[m] for m in catalog if m in result}
        path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{code}: {len(ordered)}/{len(catalog)} câu ({len(todo)} mới){f', {len(failed)} lỗi: {failed[:3]}' if failed else ''}")


if __name__ == "__main__":
    main()
