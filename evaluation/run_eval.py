"""Retrieval evaluation harness.

Runs every dataset case through kb_search and reports recall@k, MRR, nDCG,
citation correctness, duplicate rate, and latency percentiles. `--check` compares
the run against a stored baseline and fails on regression, which is the point of
the whole exercise: retrieval changes are easy to justify in the abstract and
hard to judge without numbers.

Reports are reproducible given the same corpus and dataset — the report records
the dataset name/version and every per-case number, not just the aggregate, so a
regression can be traced to the case that caused it.

Usage:
  python -m evaluation.run_eval                     # report to stdout
  python -m evaluation.run_eval --json report.json  # machine-readable report
  python -m evaluation.run_eval --update-baseline   # record current as baseline
  python -m evaluation.run_eval --check             # fail on regression
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mcp_gateway.schemas import SearchResponse
from mcp_gateway.tools.kb_search import kb_search_response

from .dataset import Case, Dataset, load_dataset
from .metrics import (
    citation_correctness,
    duplicate_rate,
    ndcg_at_k,
    percentile,
    recall_at_k,
    reciprocal_rank,
)

SearchFn = Callable[[str, int], Awaitable[SearchResponse]]

_HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = _HERE / "datasets" / "retrieval_v1.yaml"
DEFAULT_BASELINE = _HERE / "baseline.json"
DEFAULT_K = 5

# How far a metric may fall below baseline before it counts as a regression.
# Retrieval is not deterministic across corpus rebuilds, so a zero-tolerance gate
# would fail on noise and get switched off — which is worse than a loose gate.
TOLERANCE = 0.02


@dataclass
class CaseReport:
    id: str
    query: str
    recall_at_k: float
    reciprocal_rank: float
    ndcg_at_k: float
    duplicate_rate: float
    citation_correctness: float | None
    latency_ms: float
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Report:
    dataset: str
    dataset_version: int
    k: int
    cases: list[CaseReport]
    summary: dict[str, float]


async def _evaluate_case(case: Case, k: int, search_fn: SearchFn) -> CaseReport:
    started = time.perf_counter()
    response = await search_fn(case.query, k)
    latency_ms = (time.perf_counter() - started) * 1000

    citations = [r.citation for r in response.results]
    identifiers = [r.id or r.citation for r in response.results]
    cited = [(r.citation, r.excerpt) for r in response.results]

    return CaseReport(
        id=case.id,
        query=case.query,
        recall_at_k=recall_at_k(citations, case.expected_sources, k),
        reciprocal_rank=reciprocal_rank(citations, case.expected_sources),
        ndcg_at_k=ndcg_at_k(citations, case.expected_sources, k),
        duplicate_rate=duplicate_rate(identifiers),
        citation_correctness=citation_correctness(
            cited, case.expected_sources, case.answer_passages
        ),
        latency_ms=latency_ms,
        warnings=list(response.warnings),
        error=response.error.code if response.error else None,
    )


def _summarize(cases: list[CaseReport], k: int) -> dict[str, float]:
    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    latencies = [c.latency_ms for c in cases]
    correctness = [c.citation_correctness for c in cases if c.citation_correctness is not None]
    return {
        f"recall@{k}": mean([c.recall_at_k for c in cases]),
        "mrr": mean([c.reciprocal_rank for c in cases]),
        f"ndcg@{k}": mean([c.ndcg_at_k for c in cases]),
        "citation_correctness": mean(correctness),
        "duplicate_rate": mean([c.duplicate_rate for c in cases]),
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "error_rate": mean([1.0 if c.error else 0.0 for c in cases]),
    }


async def run(
    dataset: Dataset, *, k: int = DEFAULT_K, search_fn: SearchFn | None = None
) -> Report:
    search_fn = search_fn or kb_search_response
    # Cases run in sequence on purpose: latency percentiles from concurrent runs
    # would measure contention, not retrieval.
    cases = [await _evaluate_case(case, k, search_fn) for case in dataset.cases]
    return Report(
        dataset=dataset.name,
        dataset_version=dataset.version,
        k=k,
        cases=cases,
        summary=_summarize(cases, k),
    )


# Lower is better for these; everything else is higher-is-better.
_LOWER_IS_BETTER = {"duplicate_rate", "error_rate", "latency_p50_ms", "latency_p95_ms"}
# Latency is environment-dependent (cold caches, a busy workstation), so it is
# reported but not gated. Gating it here would make the suite fail for reasons
# that have nothing to do with retrieval quality.
_UNGATED = {"latency_p50_ms", "latency_p95_ms"}


def compare(summary: dict[str, float], baseline: dict[str, float]) -> list[str]:
    """Return a regression message per metric that fell outside tolerance."""
    regressions = []
    for metric, base in baseline.items():
        if metric in _UNGATED or metric not in summary:
            continue
        current = summary[metric]
        if metric in _LOWER_IS_BETTER:
            if current > base + TOLERANCE:
                regressions.append(f"{metric}: {current:.3f} worse than baseline {base:.3f}")
        elif current < base - TOLERANCE:
            regressions.append(f"{metric}: {current:.3f} below baseline {base:.3f}")
    return regressions


def format_report(report: Report) -> str:
    lines = [
        f"dataset: {report.dataset} (v{report.dataset_version}), k={report.k}, "
        f"{len(report.cases)} cases",
        "",
    ]
    for case in report.cases:
        correctness = (
            "n/a" if case.citation_correctness is None else f"{case.citation_correctness:.2f}"
        )
        lines.append(
            f"  {case.id:<28} recall={case.recall_at_k:.2f} rr={case.reciprocal_rank:.2f} "
            f"ndcg={case.ndcg_at_k:.2f} cite={correctness} {case.latency_ms:.0f}ms"
        )
        if case.error:
            lines.append(f"      error: {case.error}")
        for warning in case.warnings:
            lines.append(f"      warning: {warning}")
    lines.append("")
    for metric, value in report.summary.items():
        lines.append(f"  {metric:<24} {value:.3f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval quality.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument(
        "--update-baseline", action="store_true", help="record this run as the baseline"
    )
    parser.add_argument("--check", action="store_true", help="fail if metrics regress")
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    report = asyncio.run(run(dataset, k=args.k))
    print(format_report(report))

    if args.json:
        args.json.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")

    if args.update_baseline:
        args.baseline.write_text(
            json.dumps(
                {
                    "dataset": report.dataset,
                    "dataset_version": report.dataset_version,
                    "k": report.k,
                    "summary": report.summary,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nbaseline written to {args.baseline}")
        return

    if args.check:
        if not args.baseline.exists():
            raise SystemExit(
                f"no baseline at {args.baseline} — run with --update-baseline first"
            )
        stored = json.loads(args.baseline.read_text(encoding="utf-8"))
        if stored.get("dataset_version") != report.dataset_version:
            raise SystemExit(
                f"baseline is for dataset v{stored.get('dataset_version')}, "
                f"ran v{report.dataset_version} — regenerate the baseline"
            )
        regressions = compare(report.summary, stored.get("summary", {}))
        if regressions:
            raise SystemExit("retrieval regressed:\n  " + "\n  ".join(regressions))
        print("\nno regression against baseline")


if __name__ == "__main__":
    main()
