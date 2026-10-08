from __future__ import annotations

import json
import os
from typing import Sequence

from .schemas import Claim, ContextBlock, Generation


class BGEM3Encoder:
    """BGE-M3 adapter exposing dense, learned-sparse and ColBERT vectors."""

    def __init__(self, model_name: str, device: str = "cuda", use_fp16: bool = True):
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:  # pragma: no cover - depends on optional runtime
            raise RuntimeError("Install FlagEmbedding to use BGE-M3 retrieval") from exc
        self.model = BGEM3FlagModel(model_name, use_fp16=use_fp16, devices=device)

    def encode(self, texts: Sequence[str], batch_size: int = 12) -> dict:
        return self.model.encode(
            list(texts),
            batch_size=batch_size,
            max_length=8192,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=True,
        )


class BGEReranker:
    def __init__(self, model_name: str, device: str = "cuda", use_fp16: bool = True):
        try:
            from FlagEmbedding import FlagReranker
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install FlagEmbedding to use the neural reranker") from exc
        self.model = FlagReranker(model_name, use_fp16=use_fp16, devices=device)

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        values = self.model.compute_score([[query, text] for text in texts], normalize=True)
        if isinstance(values, float):
            return [values]
        return [float(value) for value in values]


class NliVerifier:
    """Entailment scorer used to reject unsupported generated claims."""

    def __init__(self, model_name: str, device: str = "cuda"):
        try:
            from transformers import pipeline
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install transformers to use NLI verification") from exc
        pipeline_device = 0 if device.startswith("cuda") else -1
        self.classifier = pipeline(
            "text-classification", model=model_name, device=pipeline_device, top_k=None
        )
        labels = getattr(getattr(self.classifier, "model", None), "config", None)
        id2label = getattr(labels, "id2label", {}) or {}
        self.entailment_index = self._find_label_index(id2label, "entail")
        self.contradiction_index = self._find_label_index(id2label, "contradict")
        if self.entailment_index is None:
            raise ValueError(
                "NLI model config.id2label does not identify an entailment label; "
                "refusing to use a non-entailment fallback as support."
            )
        self.entailment_label = str(id2label[self.entailment_index]).lower()
        self.contradiction_label = (
            str(id2label[self.contradiction_index]).lower()
            if self.contradiction_index is not None else None
        )

    def support_score(self, claim: str, evidence: str) -> float:
        return self.score_details(claim, evidence)[0]

    def score_details(self, claim: str, evidence: str) -> tuple[float, float]:
        result = self.classifier({"text": evidence, "text_pair": claim})
        by_label = {
            str(item["label"]).lower(): float(item["score"])
            for item in self._label_items(result)
        }
        entailment = self._score_for_label(by_label, self.entailment_label)
        contradiction = (
            self._score_for_label(by_label, self.contradiction_label)
            if self.contradiction_label is not None else 0.0
        )
        return entailment, contradiction

    @staticmethod
    def _find_label_index(id2label, marker: str):
        return next(
            (index for index, label in id2label.items() if marker in str(label).lower()),
            None,
        )

    @staticmethod
    def _score_for_label(by_label: dict[str, float], expected: str) -> float:
        for label, score in by_label.items():
            if label == expected or expected in label or label in expected:
                return score
        raise ValueError(
            f"NLI response lacks configured label {expected!r}; refusing unsafe fallback."
        )

    @staticmethod
    def _label_items(result):
        # transformers >=5 returns a flat list of {label, score} dicts for a
        # single dict input; some 4.x versions nest it one level deeper.
        if isinstance(result, dict):
            return [result]
        first = result[0] if result else None
        if isinstance(first, list):
            return first
        return list(result)


class OpenAIStructuredGenerator:
    """Grounded generator that requires claim-level context identifiers."""

    def __init__(self, model_name: str, api_key: str | None = None, temperature: float = 0.0):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install openai to use OpenAI generation") from exc
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model_name = model_name
        self.temperature = temperature

    def generate(self, query: str, context: Sequence[ContextBlock]) -> Generation:
        if not context:
            return Generation(False, reason="No supplied context.")
        prompt = _grounded_prompt(query, context)
        context_ids = [block.context_id for block in context]
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": _generation_schema(context_ids),
            },
            temperature=self.temperature,
        )
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            return _invalid_generation("provider_refusal", str(message.refusal))
        try:
            return _generation_from_payload(json.loads(message.content), context_ids)
        except (TypeError, json.JSONDecodeError) as exc:
            return _invalid_generation("provider_invalid_json", str(exc))


class LocalStructuredGenerator:
    """Offline grounded generator from a local instruction-tuned LLM.

    Used when no LLM API quota is available (controlled runs). The model must
    support a chat template; output JSON is parsed leniently and validated by
    the same citation contract as API generators, so format failures stay
    visible as validation telemetry instead of silent drops.
    """

    def __init__(self, model_name: str = "Qwen/Qwen2.5-3B-Instruct",
                 device: str = "cuda", max_new_tokens: int = 512):
        self.model_name = model_name
        self.max_new_tokens = max_new_tokens
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except Exception as exc:
            raise RuntimeError(
                "Local generation requires 'transformers' and 'torch'."
            ) from exc
        use_cuda = device == "cuda" and torch.cuda.is_available()
        self.device = "cuda" if use_cuda else "cpu"
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            self.model = AutoModelForCausalLM.from_pretrained(
                model_name, dtype=torch.float16 if use_cuda else torch.float32,
                device_map="auto" if use_cuda else None,
            )
            if not use_cuda:
                self.model = self.model.to("cpu")
            self.model.eval()
        except Exception as exc:
            raise RuntimeError(
                f"Could not load generator model '{model_name}'. Check network "
                "access to huggingface.co or pre-download the weights."
            ) from exc

    def generate(self, query: str, context: Sequence[ContextBlock]) -> Generation:
        import torch

        if not context:
            return Generation(False, reason="No supplied context.")
        prompt = _grounded_prompt(query, context, json_only=True)
        context_ids = [block.context_id for block in context]
        messages = [
            {"role": "system",
             "content": "Answer with a single JSON object only. No prose."},
            {"role": "user", "content": prompt},
        ]
        inputs = self.tokenizer.apply_chat_template(
            messages, return_tensors="pt", add_generation_prompt=True,
            return_dict=True,
        )
        input_ids = inputs["input_ids"]
        attention_mask = inputs.get("attention_mask")
        if self.device == "cuda":
            input_ids = input_ids.to("cuda")
            if attention_mask is not None:
                attention_mask = attention_mask.to("cuda")
        with torch.inference_mode():
            output = self.model.generate(
                input_ids, attention_mask=attention_mask,
                max_new_tokens=self.max_new_tokens, do_sample=False,
            )
        text = self.tokenizer.decode(
            output[0][input_ids.shape[1]:], skip_special_tokens=True
        )
        try:
            return _generation_from_payload(_json_payload(text), context_ids)
        except (TypeError, ValueError) as exc:
            return _invalid_generation("local_invalid_json", str(exc)[:300])


def _json_payload(text: str) -> dict:
    """Parse requested JSON even when a preview agent adds markdown prose."""
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
        if value.lower().startswith("json"):
            value = value[4:].lstrip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(value[start:end + 1])


class AntigravityStructuredGenerator:
    """Preview generator-swap adapter over Gemini's Interactions API.

    Antigravity does not guarantee structured output, so this adapter requests
    JSON-only text and parses it defensively. Keep it out of primary training;
    it is intended for the predeclared generator-robustness condition.
    """

    def __init__(self, model_name: str, api_key: str | None = None,
                 agent_name: str = "antigravity-preview-05-2026",
                 max_total_tokens: int = 20000):
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install google-genai to use Antigravity") from exc
        self.client = genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY"))
        self.model_name = model_name
        self.agent_name = agent_name
        self.max_total_tokens = max_total_tokens

    def generate(self, query: str, context: Sequence[ContextBlock]) -> Generation:
        if not context:
            return Generation(False, reason="No supplied context.")
        prompt = _grounded_prompt(query, context, json_only=True)
        response = self.client.interactions.create(
            agent=self.agent_name, input=prompt, environment="remote", tools=[],
            agent_config={"type": "antigravity", "model": self.model_name,
                          "max_total_tokens": self.max_total_tokens},
        )
        try:
            return _generation_from_payload(
                _json_payload(response.output_text), [block.context_id for block in context]
            )
        except (TypeError, json.JSONDecodeError) as exc:
            return _invalid_generation("provider_invalid_json", str(exc))


class GeminiStructuredGenerator:
    """Grounded generator that requires claim-level context identifiers."""

    def __init__(self, model_name: str, api_key: str | None = None):
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install google-genai to use Gemini generation") from exc
        self.client = genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY"))
        self.model_name = model_name
        self.temperature = 0.0

    def generate(self, query: str, context: Sequence[ContextBlock]) -> Generation:
        if not context:
            return Generation(False, reason="No supplied context.")
        prompt = _grounded_prompt(query, context)
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "temperature": self.temperature,
            },
        )
        try:
            return _generation_from_payload(
                json.loads(response.text), [block.context_id for block in context]
            )
        except (TypeError, json.JSONDecodeError) as exc:
            return _invalid_generation("provider_invalid_json", str(exc))


def _grounded_prompt(
    query: str, context: Sequence[ContextBlock], *, json_only: bool = False
) -> str:
    evidence = "\n\n".join(
        f"[{block.context_id}] {block.source}, pages {block.page_start}-{block.page_end}\n{block.text}"
        for block in context
    )
    id_list = ", ".join(block.context_id for block in context)
    prefix = "Return only valid JSON without markdown fences. " if json_only else ""
    return f"""Answer the scientific question only from the supplied evidence.
{prefix}Return JSON with keys answerable (boolean), reason (string), and claims (array).
Each claim must contain text, citations (context IDs), and confidence from 0 to 1.
CRITICAL: citations must use only these bare context-ID strings: {id_list}.
For example, use "C1", not "[C1]" and not the numeric index "1".
Every claim requires at least one valid citation. If evidence is insufficient,
set answerable=false and return no claims.

Question: {query}

Evidence:
{evidence}
"""


def _generation_schema(context_ids: Sequence[str]) -> dict:
    """Build the OpenAI Structured Outputs schema for this exact context."""
    return {
        "name": "grounded_generation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["answerable", "reason", "claims"],
            "properties": {
                "answerable": {"type": "boolean"},
                "reason": {"type": "string"},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text", "citations", "confidence"],
                        "properties": {
                            "text": {"type": "string"},
                            "citations": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "enum": list(context_ids)},
                            },
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                    },
                },
            },
        },
    }


def _generation_from_payload(
    payload: dict, valid_context_ids: Sequence[str] | None = None
) -> Generation:
    """Parse provider output while preserving contract violations for telemetry."""
    valid_ids = set(valid_context_ids or ())
    errors: list[str] = []
    answerable = bool(payload.get("answerable", False))
    raw_claims = payload.get("claims", [])
    if not isinstance(raw_claims, list):
        errors.append("claims_not_array")
        raw_claims = []
    claims: list[Claim] = []
    for index, item in enumerate(raw_claims):
        if not isinstance(item, dict):
            errors.append(f"claim_{index}_not_object")
            continue
        raw_citations = item.get("citations", [])
        if not isinstance(raw_citations, list):
            errors.append(f"claim_{index}_citations_not_array")
            raw_citations = []
        citations = tuple(str(value) for value in raw_citations)
        if not citations or any(not citation.strip() for citation in citations):
            errors.append(f"claim_{index}_empty_citation")
        if valid_context_ids is not None:
            unknown = sorted({citation for citation in citations if citation not in valid_ids})
            if unknown:
                errors.append(f"claim_{index}_invalid_citation:{','.join(unknown)}")
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
            errors.append(f"claim_{index}_invalid_confidence")
        claims.append(Claim(str(item.get("text", "")), citations, confidence))
    if answerable and not claims:
        errors.append("answerable_without_claims")
    if answerable and any(not claim.citations for claim in claims):
        errors.append("answerable_claim_without_citation")
    if not answerable and claims:
        errors.append("refusal_with_claims")
    return Generation(
        answerable=answerable,
        claims=tuple(claims),
        reason=str(payload.get("reason", "")),
        validation_errors=tuple(errors),
    )


def _invalid_generation(error: str, reason: str) -> Generation:
    """Surface provider-format failures as abstentions with explicit telemetry."""
    return Generation(False, reason=reason, validation_errors=(error,))
