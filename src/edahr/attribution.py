"""Claim-to-leaf attribution risk metrics.

The central quantity of the method::

    AR(S) = 1 - mean over generated claims c of max over leaf descendants e
            of P_entail(e => c)

AR is high when many generated claims cannot be attributed to any specific
child passage. It is consumed in three places:

* as a reward penalty shaping counterfactual rollout labels (:mod:`edahr.rollouts`);
* as a monitoring metric attached to every :class:`edahr.schemas.Result`;
* as the training signal for the attribution-risk predictor (future work).
"""

from __future__ import annotations

from typing import Sequence


SupportPair = tuple[str, float]


def attribution_risk(claim_supports: Sequence[SupportPair]) -> float:
    """AR(S) in [0, 1]; returns 1.0 when nothing reached the NLI stage."""
    if not claim_supports:
        return 1.0
    return 1.0 - sum(score for _, score in claim_supports) / len(claim_supports)


def unsupported_claim_rate(
    claim_supports: Sequence[SupportPair], threshold: float
) -> float:
    """Share of claims whose best child entailment stays under ``threshold``."""
    if not claim_supports:
        return 1.0
    unsupported = sum(1 for _, score in claim_supports if score < threshold)
    return unsupported / len(claim_supports)


def citation_survival_rate(generated_claims: int, verified_claims: int) -> float:
    """Fraction of generated claims that survived child-level verification."""
    if generated_claims <= 0:
        return 0.0
    return min(1.0, verified_claims / generated_claims)


def attribution_metrics(
    claim_supports: Sequence[SupportPair],
    generated_claims: int,
    verified_claims: int,
    nli_threshold: float,
) -> dict[str, float]:
    """Bundle the attribution-risk family into one flat metric dict."""
    return {
        "attribution_risk": attribution_risk(claim_supports),
        "unsupported_claim_rate": unsupported_claim_rate(claim_supports, nli_threshold),
        "citation_survival_rate": citation_survival_rate(generated_claims, verified_claims),
        "claims_scored": float(len(claim_supports)),
    }
