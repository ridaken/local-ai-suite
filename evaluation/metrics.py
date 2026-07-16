"""Retrieval metrics.

Deliberately dependency-free and pure: every function takes the citations a
search returned plus what the dataset expected, and returns a number. That makes
the metrics themselves testable, which matters — a silently wrong metric is worse
than no metric, because it licenses regressions.

Relevance is binary here (a citation is expected or it isn't). Graded relevance
would need a judgement per (query, passage) pair that this dataset does not have,
and inventing gradings would make nDCG look precise while measuring nothing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def recall_at_k(retrieved: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Fraction of expected citations present in the top k.

    Answers "did we find the material at all" — the question that matters most,
    since no amount of reranking can recover a source that was never retrieved.
    """
    if not expected:
        return 1.0
    top = set(retrieved[:k])
    return sum(1 for e in expected if e in top) / len(expected)


def reciprocal_rank(retrieved: Sequence[str], expected: Sequence[str]) -> float:
    """1/rank of the first expected citation, else 0."""
    wanted = set(expected)
    for position, citation in enumerate(retrieved, start=1):
        if citation in wanted:
            return 1.0 / position
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Normalized discounted cumulative gain over binary relevance."""
    if not expected:
        return 1.0
    wanted = set(expected)
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, citation in enumerate(retrieved[:k], start=1)
        if citation in wanted
    )
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(wanted), k) + 1))
    return dcg / ideal if ideal else 0.0


def duplicate_rate(identifiers: Sequence[str]) -> float:
    """Fraction of results that repeat one already returned.

    Duplicates are worse than merely untidy: they crowd out distinct sources
    within the top k the model actually reads.
    """
    if not identifiers:
        return 0.0
    return 1.0 - (len(set(identifiers)) / len(identifiers))


def citation_correctness(
    cited: Sequence[tuple[str, str]], expected: Sequence[str], answer_passages: Sequence[str]
) -> float | None:
    """Of the results citing an expected source, how many actually carry the
    answer text.

    This is the metric that catches the failure mode the product cares about:
    returning a real, plausible citation attached to an excerpt that does not
    support the claim. Returns None when the case retrieved no expected source
    at all — that is a recall failure and is reported as such, and folding it in
    here would double-count it.
    """
    if not answer_passages:
        return None
    wanted = set(expected)
    on_target = [(cid, text) for cid, text in cited if cid in wanted]
    if not on_target:
        return None
    hits = sum(
        1
        for _cid, text in on_target
        if any(passage.lower() in text.lower() for passage in answer_passages)
    )
    return hits / len(on_target)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile (fraction in 0..1)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
