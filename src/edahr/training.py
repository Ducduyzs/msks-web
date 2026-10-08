"""Train the attribution-risk merge policy from counterfactual rollout rows.

Input: JSONL produced by :mod:`edahr.rollouts`. Each row carries the 10-dim
``MergeFeatures`` vector of a candidate parent and binary labels for the two
level steps (``label_parent``, ``label_section``) derived from counterfactual
rewards. Queries are grouped before splitting so no query leaks across the
train/holdout boundary.

Output: a TorchScript MLP emitting merge logits; :class:`AdaptiveMergePolicy`
loads it via ``checkpoint=`` and applies sigmoid to get P(merge), which then
*replaces* the hand-tuned utility comparison inside ``decide_candidates``.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
import random
from pathlib import Path
from typing import Sequence


FEATURE_DIM = 13


def load_rollout_rows(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _labels(rows: Sequence[dict], key: str) -> tuple[list[list[float]], list[int]]:
    features = []
    for row in rows:
        vector = [float(v) for v in row["features"][:FEATURE_DIM]]
        while len(vector) < FEATURE_DIM:
            vector.append(0.0)
        features.append(vector)
    targets = [int(row[key]) for row in rows]
    return features, targets


def group_split(
    rows: Sequence[dict], val_ratio: float = 0.25, seed: int = 42,
    by: str = "query",
) -> tuple[list[dict], list[dict]]:
    """Split rows so no group of `by` (query or paper source) straddles sides."""
    keys = sorted({str(row.get(by) or row["query"]) for row in rows})
    rng = random.Random(seed)
    rng.shuffle(keys)
    holdout = set(keys[: max(1, round(len(keys) * val_ratio))])
    train = [row for row in rows if str(row.get(by) or row["query"]) not in holdout]
    val = [row for row in rows if str(row.get(by) or row["query"]) in holdout]
    return train, val


def v5_label(row: dict, branch: str, epsilon: float, delta: float, tau: float) -> int:
    """Constrained oracle: EXPAND only when precision is preserved within
    ``epsilon`` of KEEP, harmful drift stays under ``delta``, and the reward
    improves by more than ``tau``."""
    keep = row["branches"]["keep"]
    expand = row["branches"].get(branch)
    if not expand or "v5" not in keep or "v5" not in expand:
        return 0
    feasible = (
        expand["v5"]["citation_precision"] >= keep["v5"]["citation_precision"] - epsilon
        and expand["v5"]["citation_recall"] >= keep["v5"]["citation_recall"] - epsilon
        and expand["v5"]["harmful_rate"] <= delta
        and not (
            bool(expand["v5"].get("empty_evidence"))
            and not bool(keep["v5"].get("empty_evidence"))
        )
    )
    gain = expand["reward"] - keep["reward"]
    return int(feasible and gain > tau)


def relabel_v5(
    rows: Sequence[dict], branch: str, epsilon: float = 0.02,
    delta: float = 0.05, tau: float = 0.02,
) -> list[dict]:
    """Return copies of rows whose label for `branch` follows the constrained rule."""
    key = f"label_{branch}" if branch != "section" else "label_section"
    out = []
    for row in rows:
        copy = dict(row)
        copy[key] = v5_label(row, branch, epsilon, delta, tau)
        out.append(copy)
    return out


def _accuracy(logits: list[float], labels: Sequence[int], threshold: float = 0.5) -> float:
    correct = sum(
        1 for logit, label in zip(logits, labels) if (logit >= threshold) == bool(label)
    )
    return correct / max(1, len(labels))


def _rank_auc(logits: list[float], labels: Sequence[int]) -> float:
    positives = [(logit, 1) for logit, label in zip(logits, labels) if label]
    negatives = [(logit, 0) for logit, label in zip(logits, labels) if not label]
    if not positives or not negatives:
        return 0.5
    wins = ties = 0
    for pos_logit, _ in positives:
        for neg_logit, _ in negatives:
            if pos_logit > neg_logit:
                wins += 1
            elif pos_logit == neg_logit:
                ties += 1
    return (wins + 0.5 * ties) / (len(positives) * len(negatives))


def _brier_score(probs: list[float], labels: Sequence[int]) -> float:
    """Brier score (mean squared error of probabilistic predictions)."""
    return sum((p - y) ** 2 for p, y in zip(probs, labels)) / max(1, len(labels))


def _ece(probs: list[float], labels: Sequence[int], n_bins: int = 10) -> float:
    """Expected Calibration Error."""
    if not probs:
        return 0.0
    bin_counts = [0] * n_bins
    bin_conf = [0.0] * n_bins
    bin_acc = [0.0] * n_bins
    for p, y in zip(probs, labels):
        idx = min(int(p * n_bins), n_bins - 1)
        bin_counts[idx] += 1
        bin_conf[idx] += p
        bin_acc[idx] += y
    ece = 0.0
    total = len(probs)
    for i in range(n_bins):
        if bin_counts[i] > 0:
            avg_conf = bin_conf[i] / bin_counts[i]
            avg_acc = bin_acc[i] / bin_counts[i]
            ece += (bin_counts[i] / total) * abs(avg_acc - avg_conf)
    return ece


def _paper_grouped_auc(
    logits: list[float], labels: Sequence[int], sources: Sequence[str]
) -> tuple[float, tuple[float, float]]:
    """Compute paper-grouped AUC with clustered bootstrap CI.

    Returns (auc, (ci_low, ci_high)) where CI is 95% clustered by source.
    """
    if not logits or not labels or not sources:
        return 0.5, (0.5, 0.5)

    def _auc_for_indices(indices: list[int]) -> float:
        sub_logits = [logits[i] for i in indices]
        sub_labels = [labels[i] for i in indices]
        return _rank_auc(sub_logits, sub_labels)

    auc = _rank_auc(logits, labels)

    by_source: dict[str, list[int]] = {}
    for i, src in enumerate(sources):
        by_source.setdefault(str(src), []).append(i)
    groups = list(by_source.values())

    if len(groups) < 2:
        return auc, (auc, auc)

    rng = random.Random(42)
    n_groups = len(groups)
    bootstrap_aucs: list[float] = []
    for _ in range(1000):
        sampled_groups = [groups[rng.randrange(n_groups)] for _ in range(n_groups)]
        sampled_indices = [idx for g in sampled_groups for idx in g]
        if len(sampled_indices) < 2:
            continue
        bootstrap_aucs.append(_auc_for_indices(sampled_indices))

    if not bootstrap_aucs:
        return auc, (auc, auc)

    bootstrap_aucs.sort()
    low = bootstrap_aucs[int(0.025 * len(bootstrap_aucs))]
    high = bootstrap_aucs[min(len(bootstrap_aucs) - 1, int(0.975 * len(bootstrap_aucs)))]
    return auc, (low, high)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _logits_to_probs(logits: list[float]) -> list[float]:
    return [_logits_to_probs_single(l) for l in logits]


def _logits_to_probs_single(logit: float) -> float:
    return 1.0 / (1.0 + math.exp(-logit))


def train_label(
    rows: Sequence[dict],
    label_key: str,
    epochs: int = 400,
    patience: int = 60,
    lr: float = 2e-3,
    weight_decay: float = 1e-4,
    hidden: int = 16,
    seed: int = 42,
    min_margin: float = 0.0,
    min_auc: float = 0.60,
    require_ci_exclude_half: bool = True,
) -> tuple[object, dict]:
    """Fit a small MLP on one binary level-step label; returns (model, report).

    ``min_margin`` drops rows whose reward gap between the compared branches is
    ambiguous (|gap| <= min_margin), trading coverage for label cleanliness.

    Dev acceptance criteria (fail-fast):
    - Paper-grouped AUC > ``min_auc`` (default 0.60)
    - 95% clustered CI of AUC does not contain 0.50
    - Reports Brier score and ECE for calibration monitoring
    """
    import torch
    from torch import nn

    gap_keys = {
        "label_parent": ("parent", "keep"),
        "label_section": ("section", "keep"),
    }

    def _gap(row: dict) -> float:
        branch, base = gap_keys[label_key]
        return row["branches"].get(branch, {}).get("reward", 0.0) - \
            row["branches"].get(base, {}).get("reward", 0.0)

    if min_margin > 0.0:
        kept = [row for row in rows if abs(_gap(row)) > min_margin]
    else:
        kept = list(rows)
    torch.manual_seed(seed)
    train_rows, val_rows = group_split(kept, by="source")
    x_train, y_train = _labels(train_rows, label_key)
    x_val, y_val = _labels(val_rows, label_key)
    if not x_train or not x_val:
        raise ValueError(f"not enough labelled rows for {label_key}")

    x_t = torch.tensor(x_train, dtype=torch.float32)
    y_t = torch.tensor(y_train, dtype=torch.float32).unsqueeze(1)
    x_v = torch.tensor(x_val, dtype=torch.float32)

    model: nn.Module = nn.Sequential(
        nn.Linear(FEATURE_DIM, hidden),
        nn.ReLU(),
        nn.Linear(hidden, 1),
    )
    optimiser = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    loss_fn = nn.BCEWithLogitsLoss()

    best_val_loss = float("inf")
    best_state = {k: v.clone() for k, v in model.state_dict().items()}
    stale = 0
    for epoch in range(epochs):
        model.train()
        optimiser.zero_grad()
        loss = loss_fn(model(x_t), y_t)
        loss.backward()
        optimiser.step()
        model.eval()
        with torch.inference_mode():
            val_logits = model(x_v).flatten().tolist()
        val_loss = loss_fn(torch.tensor(val_logits).unsqueeze(1),
                           torch.tensor(y_val, dtype=torch.float32).unsqueeze(1))
        if float(val_loss) < best_val_loss - 1e-5:
            best_val_loss = float(val_loss)
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= patience:
                break
    model.load_state_dict(best_state)
    model.eval()
    with torch.inference_mode():
        train_logits = model(x_t).flatten().tolist()
        val_logits = model(x_v).flatten().tolist()

    train_probs = _logits_to_probs(train_logits)
    val_probs = _logits_to_probs(val_logits)

    positive_rate = sum(y_train) / len(y_train)
    val_auc = _rank_auc(val_logits, y_val)
    val_sources = [str(row.get("source") or row["query"]) for row in val_rows]
    paper_auc, (ci_low, ci_high) = _paper_grouped_auc(val_logits, y_val, val_sources)
    val_brier = _brier_score(val_probs, y_val)
    val_ece = _ece(val_probs, y_val)

    ci_excludes_half = not (ci_low <= 0.5 <= ci_high)
    auc_passes = paper_auc > min_auc
    ci_passes = ci_excludes_half if require_ci_exclude_half else True

    report = {
        "label": label_key,
        "rows_total": len(rows),
        "rows_kept_min_margin": len(kept),
        "train_rows": len(x_train),
        "val_rows": len(x_val),
        "epochs_run": epoch + 1,
        "positive_rate_train": round(positive_rate, 4),
        "train_acc": round(_accuracy(train_logits, y_train), 4),
        "val_acc": round(_accuracy(val_logits, y_val), 4),
        "val_auc": round(val_auc, 4),
        "paper_grouped_auc": round(paper_auc, 4),
        "paper_auc_ci_low": round(ci_low, 4),
        "paper_auc_ci_high": round(ci_high, 4),
        "ci_excludes_half": ci_excludes_half,
        "val_brier": round(val_brier, 4),
        "val_ece": round(val_ece, 4),
        "always_merge_acc": round(positive_rate, 4),
        "never_merge_acc": round(1.0 - positive_rate, 4),
        "dev_criteria": {
            "min_auc": min_auc,
            "auc_passes": auc_passes,
            "ci_excludes_half_required": require_ci_exclude_half,
            "ci_passes": ci_passes,
            "all_pass": auc_passes and ci_passes,
        },
    }

    if not (auc_passes and ci_passes):
        reasons = []
        if not auc_passes:
            reasons.append(f"paper-grouped AUC {paper_auc:.4f} <= {min_auc}")
        if not ci_passes:
            reasons.append(f"clustered CI [{ci_low:.4f}, {ci_high:.4f}] contains 0.50")
        raise RuntimeError(
            f"Dev criteria not met for {label_key}: {'; '.join(reasons)}. "
            f"Brier={val_brier:.4f}, ECE={val_ece:.4f}."
        )

    return model, report


def export_checkpoint(model: object, out_path: str | Path) -> Path:
    import torch

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    scripted = torch.jit.script(model.cpu().eval())
    scripted.save(str(out))
    return out


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checkpoint_metadata(
    checkpoint: str | Path,
    report: dict,
    *,
    source_rollouts: str | Path,
    seed: int,
    min_margin: float,
    v5: bool,
    epsilon: float,
    delta: float,
    tau: float,
    out_path: str | Path | None = None,
) -> Path:
    """Write a sidecar manifest sufficient to identify a trained gate."""
    checkpoint_path = Path(checkpoint).resolve()
    rollout_path = Path(source_rollouts).resolve()
    output = (
        Path(out_path)
        if out_path is not None
        else checkpoint_path.with_suffix(".metadata.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "checkpoint": str(checkpoint_path),
        "checkpoint_sha256": sha256_file(checkpoint_path),
        "feature_dim": FEATURE_DIM,
        "source_rollouts": str(rollout_path),
        "source_rollouts_sha256": sha256_file(rollout_path),
        "seed": seed,
        "min_margin": min_margin,
        "v5_constraints": {"enabled": v5, "epsilon": epsilon, "delta": delta, "tau": tau},
        "training_report": report,
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output