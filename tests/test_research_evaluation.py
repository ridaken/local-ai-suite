"""Tests for the model/tool golden-document evaluation harness."""

import asyncio
import json
from dataclasses import asdict

import pytest

from evaluation import run_research_eval as research_eval
from evaluation.research_dataset import (
    ResearchDatasetError,
    load_research_dataset,
    load_snapshot,
)
from evaluation.run_research_eval import (
    DEFAULT_DATASET,
    DEFAULT_PROMPT,
    SnapshotTools,
    ToolEvent,
    _score_trial,
    compare_baseline,
    extract_selected_pmids,
    load_research_prompt,
    rescore_report,
    run_trial,
)


def test_shipped_research_dataset_and_snapshot_are_valid():
    dataset = load_research_dataset(DEFAULT_DATASET)
    snapshot = load_snapshot(dataset)

    assert dataset.must_select == {"29129157", "29364767"}
    assert len(dataset.candidate_pmids) == 20
    assert len(snapshot["search_response"]["results"]) == 20


def test_dataset_rejects_unjudged_candidates(tmp_path):
    path = tmp_path / "dataset.yaml"
    path.write_text(
        "version: 1\nname: test\nquestion: q\nsnapshot: s.json\n"
        'candidate_pmids: ["123456"]\njudgments: []\n',
        encoding="utf-8",
    )
    with pytest.raises(ResearchDatasetError, match="non-empty"):
        load_research_dataset(path)


def test_prompt_markers_extract_the_profile_text():
    prompt = load_research_prompt(DEFAULT_PROMPT)
    assert prompt.startswith("You are a careful research assistant")
    assert "article_find" in prompt


def test_extract_selected_pmids_uses_citations_and_deduplicates():
    text = (
        "DAWN (PMID: 29129157, https://pubmed.ncbi.nlm.nih.gov/29129157/) and "
        "DEFUSE 3 PubMed ID: 29364767 and pubmed:29364767."
    )
    assert extract_selected_pmids(text) == ["29129157", "29364767"]


def _event(name, pmid):
    return ToolEvent(
        name=name,
        arguments={"article_id": f"pubmed:{pmid}"},
        result_pmids=[pmid],
        content_level="abstract",
        error=None,
    )


def test_strict_score_requires_all_gold_and_prior_inspection():
    dataset = load_research_dataset(DEFAULT_DATASET)
    answer = (
        "DAWN https://pubmed.ncbi.nlm.nih.gov/29129157/ and DEFUSE 3 "
        "https://pubmed.ncbi.nlm.nih.gov/29364767/."
    )
    report = _score_trial(
        mode="snapshot",
        run=1,
        seed=41,
        dataset=dataset,
        final_answer=answer,
        events=[_event("article_find", "29129157"), _event("article_find", "29364767")],
        latency_ms=1,
        usage={},
        error=None,
    )
    assert report.passed
    assert report.must_select_recall == 1.0

    missing_inspection = _score_trial(
        mode="snapshot",
        run=1,
        seed=41,
        dataset=dataset,
        final_answer=answer,
        events=[_event("article_find", "29129157")],
        latency_ms=1,
        usage={},
        error=None,
    )
    assert not missing_inspection.passed
    assert missing_inspection.cited_without_inspection == ["29364767"]


def test_strict_score_rejects_a_cited_distractor():
    dataset = load_research_dataset(DEFAULT_DATASET)
    answer = " ".join(
        f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        for pmid in ("29129157", "29364767", "29807887")
    )
    report = _score_trial(
        mode="snapshot",
        run=1,
        seed=41,
        dataset=dataset,
        final_answer=answer,
        events=[
            _event("article_find", "29129157"),
            _event("article_find", "29364767"),
            _event("article_find", "29807887"),
        ],
        latency_ms=1,
        usage={},
        error=None,
    )
    assert not report.passed
    assert report.selected_distractors == ["29807887"]


def test_snapshot_tools_return_frozen_candidates_and_articles():
    dataset = load_research_dataset(DEFAULT_DATASET)
    tools = SnapshotTools(dataset, load_snapshot(dataset))

    search = asyncio.run(tools.call("pubmed_search", {"query": "anything", "limit": 3}))
    assert search.result_pmids == list(dataset.candidate_pmids[:3])
    article = asyncio.run(
        tools.call(
            "article_find",
            {"article_id": "pubmed:29129157", "query": "90 day outcome", "limit": 2},
        )
    )
    assert article.result_pmids == ["29129157"]
    assert article.content_level == "abstract"
    assert "49%" in article.rendered


def test_agent_loop_records_search_inspection_and_final_selection(monkeypatch):
    dataset = load_research_dataset(DEFAULT_DATASET)
    tools = SnapshotTools(dataset, load_snapshot(dataset))
    payloads = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "search",
                                "type": "function",
                                "function": {
                                    "name": "pubmed_search",
                                    "arguments": '{"query":"late window thrombectomy","limit":5}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": pmid,
                                "type": "function",
                                "function": {
                                    "name": "article_find",
                                    "arguments": json.dumps(
                                        {
                                            "article_id": f"pubmed:{pmid}",
                                            "query": "randomized 90 day outcome",
                                            "limit": 2,
                                        }
                                    ),
                                },
                            }
                            for pmid in ("29129157", "29364767")
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
        },
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "DAWN https://pubmed.ncbi.nlm.nih.gov/29129157/ and DEFUSE 3 "
                            "https://pubmed.ncbi.nlm.nih.gov/29364767/."
                        ),
                    }
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34},
        },
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response(payloads.pop(0))

    monkeypatch.setattr(research_eval.httpx, "AsyncClient", Client)
    report = asyncio.run(
        run_trial(
            mode="snapshot",
            run_number=1,
            seed=41,
            dataset=dataset,
            prompt=load_research_prompt(DEFAULT_PROMPT),
            tools=tools,
            model_url="http://model/v1",
            model="test-model",
            max_turns=5,
            max_tokens=100,
        )
    )

    assert report.passed
    assert report.surfaced_pmids == list(dataset.candidate_pmids[:5])
    assert report.inspected_pmids == ["29129157", "29364767"]
    assert report.usage["total_tokens"] == 69


def test_baseline_comparison_allows_prompt_changes_but_rejects_corpus_changes():
    report = {
        "dataset": "d",
        "metadata": {
            "dataset_hash": "dataset",
            "snapshot_hash": "snapshot",
            "prompt_hash": "new",
            "tool_schema_hash": "tools",
            "model": {"requested": "m"},
        },
        "modes": {
            "snapshot": {"summary": {"selection_frequency": {"1": 1.0}}}
        },
    }
    baseline = {
        "schema_version": 1,
        "dataset": "d",
        "metadata": {
            "dataset_hash": "dataset",
            "snapshot_hash": "snapshot",
            "prompt_hash": "old",
            "tool_schema_hash": "tools",
            "model": {"requested": "m"},
        },
        "summaries": {"snapshot": {"selection_frequency": {"1": 0.0}}},
    }
    comparison = compare_baseline(report, baseline)
    assert comparison["compatible"]
    assert comparison["prompt_changed"]
    assert comparison["selection_frequency_delta"]["snapshot"]["1"] == 1.0

    baseline["metadata"]["snapshot_hash"] = "other"
    assert not compare_baseline(report, baseline)["compatible"]


def test_report_can_be_rescored_without_rerunning_the_model():
    dataset = load_research_dataset(DEFAULT_DATASET)
    raw = {
        "modes": {
            "live": {
                "trials": [
                    {
                        "run": 1,
                        "seed": 41,
                        "final_answer": "PubMed ID: 29129157 and pubmed:29364767",
                        "tool_events": [
                            asdict(_event("article_find", "29129157")),
                            asdict(_event("article_find", "29364767")),
                        ],
                        "latency_ms": 1,
                        "usage": {},
                        "error": None,
                    }
                ]
            }
        }
    }
    rescored = rescore_report(raw, dataset)
    assert rescored["modes"]["live"]["summary"]["all_passed"]


def test_snapshot_json_contains_no_credentials():
    dataset = load_research_dataset(DEFAULT_DATASET)
    text = dataset.snapshot_path.read_text(encoding="utf-8").lower()
    assert "authorization" not in text
    assert "bearer " not in text
    json.loads(text)
