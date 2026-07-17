"""Evaluation harness tests.

The metrics are tested against hand-computed values rather than golden output:
a metric that is silently wrong scores every future change against the wrong
target, which is worse than having no metric at all.
"""

import asyncio
import math

import pytest

from evaluation.dataset import DatasetError, load_dataset
from evaluation.metrics import (
    citation_correctness,
    duplicate_rate,
    ndcg_at_k,
    percentile,
    recall_at_k,
    reciprocal_rank,
)
from evaluation.run_eval import DEFAULT_DATASET, compare, run
from mcp_gateway.schemas import SearchResponse, SearchResult, search_error

# --- metrics ------------------------------------------------------------------


def test_recall_counts_expected_within_k():
    retrieved = ["a", "b", "c", "d"]
    assert recall_at_k(retrieved, ["a", "c"], 4) == 1.0
    assert recall_at_k(retrieved, ["a", "d"], 2) == 0.5  # d falls outside k
    assert recall_at_k(retrieved, ["z"], 4) == 0.0


def test_reciprocal_rank_uses_first_hit():
    assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert reciprocal_rank(["a", "x"], ["a"]) == 1.0
    assert reciprocal_rank(["x", "y"], ["a"]) == 0.0


def test_ndcg_rewards_earlier_hits():
    early = ndcg_at_k(["a", "x", "y"], ["a"], 3)
    late = ndcg_at_k(["x", "y", "a"], ["a"], 3)
    assert early == 1.0
    assert late < early
    assert late == pytest.approx(1.0 / math.log2(4))


def test_ndcg_is_one_when_all_expected_lead():
    assert ndcg_at_k(["a", "b", "x"], ["a", "b"], 3) == pytest.approx(1.0)


def test_duplicate_rate():
    assert duplicate_rate(["a", "b", "c"]) == 0.0
    assert duplicate_rate(["a", "a", "b", "b"]) == 0.5
    assert duplicate_rate([]) == 0.0


def test_citation_correctness_flags_a_citation_that_lacks_the_answer():
    cited = [("a.py:1", "the event loop runs coroutines"), ("a.py:2", "unrelated boilerplate")]
    # Both cite an expected source, but only one carries the answer.
    assert citation_correctness(cited, ["a.py:1", "a.py:2"], ["event loop"]) == 0.5


def test_citation_correctness_ignores_results_outside_expected_sources():
    cited = [("a.py:1", "event loop"), ("junk.py:9", "noise")]
    assert citation_correctness(cited, ["a.py:1"], ["event loop"]) == 1.0


def test_citation_correctness_is_none_when_nothing_expected_was_retrieved():
    # That is a recall failure; folding it in here would double-count it.
    assert citation_correctness([("x.py:1", "text")], ["a.py:1"], ["event loop"]) is None


def test_percentile():
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 0.5) == 25.0
    assert percentile(values, 1.0) == 40.0
    assert percentile([7.0], 0.95) == 7.0
    assert percentile([], 0.5) == 0.0


# --- dataset ------------------------------------------------------------------


def _write(tmp_path, body):
    path = tmp_path / "ds.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_shipped_dataset_is_valid():
    dataset = load_dataset(DEFAULT_DATASET)
    assert dataset.cases
    assert all(c.expected_sources for c in dataset.cases)


def test_unsupported_version_is_rejected(tmp_path):
    path = _write(tmp_path, "version: 99\ncases:\n  - id: a\n    query: q\n")
    with pytest.raises(DatasetError, match="not supported"):
        load_dataset(path)


def test_case_without_expected_sources_is_rejected(tmp_path):
    # A case expecting nothing always scores 1.0 and would mask a regression.
    path = _write(tmp_path, "version: 1\ncases:\n  - id: a\n    query: q\n")
    with pytest.raises(DatasetError, match="no expected_sources"):
        load_dataset(path)


def test_duplicate_case_ids_are_rejected(tmp_path):
    path = _write(
        tmp_path,
        "version: 1\ncases:\n"
        "  - id: a\n    query: q\n    expected_sources: [x]\n"
        "  - id: a\n    query: r\n    expected_sources: [y]\n",
    )
    with pytest.raises(DatasetError, match="duplicate case id"):
        load_dataset(path)


def test_empty_cases_list_is_rejected(tmp_path):
    with pytest.raises(DatasetError, match="non-empty"):
        load_dataset(_write(tmp_path, "version: 1\ncases: []\n"))


# --- runner -------------------------------------------------------------------


def _dataset(tmp_path):
    return load_dataset(
        _write(
            tmp_path,
            "version: 1\nname: t\ncases:\n"
            "  - id: hit\n    query: async\n"
            "    expected_sources: [a.py:1]\n"
            "    answer_passages: [event loop]\n"
            "  - id: miss\n    query: nothing\n"
            "    expected_sources: [z.py:1]\n",
        )
    )


def _result(citation, excerpt):
    return SearchResult(
        id=citation, title="t", excerpt=excerpt, source_kind="curated", citation=citation
    )


def test_run_reports_per_case_and_summary(tmp_path):
    async def fake_search(query, k):
        if query == "async":
            return SearchResponse(
                query=query, results=[_result("a.py:1", "the event loop runs coroutines")]
            )
        return SearchResponse(query=query, results=[_result("other.py:1", "noise")])

    report = asyncio.run(run(_dataset(tmp_path), k=5, search_fn=fake_search))

    by_id = {c.id: c for c in report.cases}
    assert by_id["hit"].recall_at_k == 1.0
    assert by_id["hit"].citation_correctness == 1.0
    assert by_id["miss"].recall_at_k == 0.0
    assert report.summary["recall@5"] == 0.5
    assert report.summary["mrr"] == 0.5
    assert report.dataset_version == 1
    assert report.summary["latency_p95_ms"] >= 0


def test_run_records_errors_and_warnings(tmp_path):
    async def failing(query, k):
        if query == "async":
            return SearchResponse(
                query=query,
                results=[_result("a.py:1", "event loop")],
                warnings=["vector tier unavailable (ConnectError)"],
            )
        return search_error(query, "retrieval_unavailable", "everything is down")

    report = asyncio.run(run(_dataset(tmp_path), k=5, search_fn=failing))
    by_id = {c.id: c for c in report.cases}
    assert by_id["hit"].warnings == ["vector tier unavailable (ConnectError)"]
    assert by_id["miss"].error == "retrieval_unavailable"
    assert report.summary["error_rate"] == 0.5


def test_compare_flags_a_drop_beyond_tolerance():
    baseline = {"recall@5": 0.80, "duplicate_rate": 0.00}
    assert compare({"recall@5": 0.80, "duplicate_rate": 0.0}, baseline) == []
    # Within tolerance: noise, not a regression.
    assert compare({"recall@5": 0.79, "duplicate_rate": 0.0}, baseline) == []
    regressions = compare({"recall@5": 0.50, "duplicate_rate": 0.0}, baseline)
    assert len(regressions) == 1
    assert "recall@5" in regressions[0]


def test_compare_treats_duplicate_rate_as_lower_is_better():
    baseline = {"duplicate_rate": 0.0}
    assert compare({"duplicate_rate": 0.5}, baseline)
    assert compare({"duplicate_rate": 0.0}, baseline) == []


def test_compare_ignores_latency():
    # Latency depends on the machine; gating it would fail for reasons unrelated
    # to retrieval quality.
    assert compare({"latency_p95_ms": 9999.0}, {"latency_p95_ms": 10.0}) == []
