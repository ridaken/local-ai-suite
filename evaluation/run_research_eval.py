"""Evaluate whether a tool-using model selects adjudicated PubMed documents.

The deterministic snapshot mode isolates model/prompt selection behavior. Live
mode exercises the gateway and current PubMed ranking, but is diagnostic because
the upstream corpus changes. Reports deliberately retain tool metadata rather
than source text or hidden reasoning.

Examples:
  python -m evaluation.run_research_eval --capture-snapshot
  python -m evaluation.run_research_eval --mode both --repeats 3
  python -m evaluation.run_research_eval --mode snapshot --check
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from mcp_gateway.schemas import (
    ArticlePassage,
    ArticlePassageResponse,
    ArticleReadResponse,
    SearchResponse,
    render_article_passages,
    render_article_read,
    render_search,
)

from .research_dataset import (
    ResearchDataset,
    ResearchDatasetError,
    load_research_dataset,
    load_snapshot,
    stable_hash,
)

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
DEFAULT_DATASET = _HERE / "datasets" / "pubmed_selection_v1.yaml"
DEFAULT_PROMPT = _ROOT / "docs" / "prompts.md"
DEFAULT_BASELINE = _HERE / "baselines" / "pubmed_selection_v1.json"
DEFAULT_MODEL_URL = "http://localhost:8001/v1"
DEFAULT_MODEL = "Qwen-3.6-35B-MoE-Thinking"
DEFAULT_GATEWAY_URL = "http://localhost:8090"
DEFAULT_KEY_FILE = _ROOT / "config" / "secrets" / "mcp_api_key.txt"
PROMPT_START = "<!-- research-verify-prompt:start -->"
PROMPT_END = "<!-- research-verify-prompt:end -->"
_PUBMED_URL = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,12})", re.I)
_PMID_TEXT = re.compile(r"\b(?:PMID|PubMed\s+ID|pubmed)\s*:?\s*(\d{6,12})\b", re.I)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_research_prompt(path: Path) -> str:
    text = Path(path).read_text(encoding="utf-8")
    if text.count(PROMPT_START) != 1 or text.count(PROMPT_END) != 1:
        raise ValueError("research prompt markers must each appear exactly once")
    body = text.split(PROMPT_START, 1)[1].split(PROMPT_END, 1)[0].strip()
    match = re.fullmatch(r"```text\s*\n(.*)\n```", body, re.DOTALL)
    if not match:
        raise ValueError("research prompt markers must wrap one ```text code fence")
    prompt = match.group(1).strip()
    if not prompt:
        raise ValueError("research prompt is empty")
    return prompt


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "pubmed_search",
            "description": (
                "Search PubMed for biomedical article candidates and complete abstracts. "
                "Results are candidates; inspect selected article_id values before citing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "article_find",
            "description": (
                "Find query-relevant passages in a PubMed article selected from search. "
                "The response states whether evidence is full text or abstract only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "article_id": {"type": "string", "pattern": "^pubmed:[0-9]{6,12}$"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["article_id", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "article_read",
            "description": (
                "Read sequential context from a selected PubMed article. Use an offset "
                "returned by article_find or begin at zero."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "article_id": {"type": "string", "pattern": "^pubmed:[0-9]{6,12}$"},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": ["article_id"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class ToolResult:
    rendered: str
    result_pmids: list[str] = field(default_factory=list)
    content_level: str | None = None
    error: str | None = None


class ResearchTools(Protocol):
    async def call(self, name: str, arguments: dict) -> ToolResult: ...


def _article_pmid(arguments: dict) -> str | None:
    article_id = str(arguments.get("article_id", ""))
    if re.fullmatch(r"pubmed:\d{6,12}", article_id):
        return article_id.removeprefix("pubmed:")
    return None


class SnapshotTools:
    def __init__(self, dataset: ResearchDataset, snapshot: dict):
        self.dataset = dataset
        self.snapshot = snapshot

    async def call(self, name: str, arguments: dict) -> ToolResult:
        if name == "pubmed_search":
            query = str(arguments.get("query", "")).strip()
            limit = max(1, min(int(arguments.get("limit", 5)), 20))
            stored = SearchResponse.model_validate(self.snapshot["search_response"])
            response = stored.model_copy(update={"query": query, "results": stored.results[:limit]})
            return ToolResult(
                rendered=render_search(
                    response,
                    heading="PubMed results",
                    footer=(
                        "Search results are candidates, not proof. Select relevant article_id "
                        "values and call article_find/article_read before citing contents."
                    ),
                ),
                result_pmids=[item.article_id.removeprefix("pubmed:") for item in response.results],
            )
        if name not in {"article_find", "article_read"}:
            return ToolResult(rendered=f"Unknown tool: {name}", error="unknown_tool")
        pmid = _article_pmid(arguments)
        if not pmid or pmid not in self.snapshot["articles"]:
            return ToolResult(
                rendered="Article is not in the frozen benchmark corpus.", error="not_found"
            )
        article = self.snapshot["articles"][pmid]
        text = str(article["text"])
        if name == "article_find":
            response = ArticlePassageResponse(
                article_id=f"pubmed:{pmid}",
                query=str(arguments.get("query", "")),
                title=article["title"],
                citation=article["citation"],
                provider="pubmed",
                content_level=article["content_level"],
                extraction_method=article["extraction_method"],
                passages=[
                    ArticlePassage(
                        section="Abstract",
                        text=text,
                        offset=0,
                        end_offset=len(text),
                    )
                ],
            )
            return ToolResult(
                rendered=render_article_passages(response),
                result_pmids=[pmid],
                content_level=response.content_level,
            )
        offset = max(0, int(arguments.get("offset", 0)))
        if offset >= len(text):
            return ToolResult(
                rendered="Offset is past the end of the article.", error="offset_past_end"
            )
        window = text[offset : offset + 8000]
        response = ArticleReadResponse(
            article_id=f"pubmed:{pmid}",
            title=article["title"],
            citation=article["citation"],
            provider="pubmed",
            content_level=article["content_level"],
            extraction_method=article["extraction_method"],
            text=window,
            offset=offset,
            next_offset=offset + len(window) if offset + len(window) < len(text) else None,
            total_length=len(text),
            end_of_document=offset + len(window) >= len(text),
        )
        return ToolResult(
            rendered=render_article_read(response),
            result_pmids=[pmid],
            content_level=response.content_level,
        )


class LiveTools:
    _PATHS = {
        "pubmed_search": "/api/v1/pubmed/search",
        "article_find": "/api/v1/articles/find",
        "article_read": "/api/v1/articles/read",
    }

    def __init__(self, gateway_url: str, api_key: str):
        self.gateway_url = gateway_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def call(self, name: str, arguments: dict) -> ToolResult:
        if name not in self._PATHS:
            return ToolResult(rendered=f"Unknown tool: {name}", error="unknown_tool")
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                self.gateway_url + self._PATHS[name], headers=self.headers, json=arguments
            )
        try:
            payload = response.json()
        except ValueError:
            return ToolResult(
                rendered=f"Gateway returned HTTP {response.status_code} with invalid JSON.",
                error="invalid_json",
            )
        if name == "pubmed_search":
            parsed = SearchResponse.model_validate(payload)
            return ToolResult(
                rendered=render_search(
                    parsed,
                    heading="PubMed results",
                    footer=(
                        "Search results are candidates, not proof. Select relevant article_id "
                        "values and call article_find/article_read before citing contents."
                    ),
                ),
                result_pmids=[
                    item.article_id.removeprefix("pubmed:")
                    for item in parsed.results
                    if item.article_id and item.article_id.startswith("pubmed:")
                ],
                error=parsed.error.code if parsed.error else None,
            )
        if name == "article_find":
            parsed = ArticlePassageResponse.model_validate(payload)
            pmid = _article_pmid(arguments)
            return ToolResult(
                rendered=render_article_passages(parsed),
                result_pmids=[pmid] if pmid else [],
                content_level=parsed.content_level,
                error=parsed.error.code if parsed.error else None,
            )
        parsed = ArticleReadResponse.model_validate(payload)
        pmid = _article_pmid(arguments)
        return ToolResult(
            rendered=render_article_read(parsed),
            result_pmids=[pmid] if pmid else [],
            content_level=parsed.content_level,
            error=parsed.error.code if parsed.error else None,
        )


@dataclass
class ToolEvent:
    name: str
    arguments: dict
    result_pmids: list[str]
    content_level: str | None
    error: str | None


@dataclass
class TrialReport:
    mode: str
    run: int
    seed: int
    passed: bool
    must_select_recall: float
    selected_pmids: list[str]
    inspected_pmids: list[str]
    surfaced_pmids: list[str]
    missed_must_select: list[str]
    selected_supporting: list[str]
    selected_distractors: list[str]
    cited_without_inspection: list[str]
    content_levels: dict[str, str]
    tool_events: list[ToolEvent]
    final_answer: str
    latency_ms: float
    usage: dict[str, int]
    error: str | None = None


def extract_selected_pmids(text: str) -> list[str]:
    values = _PUBMED_URL.findall(text) + _PMID_TEXT.findall(text)
    return list(dict.fromkeys(values))


def _score_trial(
    *,
    mode: str,
    run: int,
    seed: int,
    dataset: ResearchDataset,
    final_answer: str,
    events: list[ToolEvent],
    latency_ms: float,
    usage: dict[str, int],
    error: str | None,
) -> TrialReport:
    selected = extract_selected_pmids(final_answer)
    inspected = list(
        dict.fromkeys(
            pmid
            for event in events
            if event.name in {"article_find", "article_read"} and not event.error
            for pmid in event.result_pmids
        )
    )
    surfaced = list(
        dict.fromkeys(
            pmid
            for event in events
            if event.name == "pubmed_search" and not event.error
            for pmid in event.result_pmids
        )
    )
    selected_set = set(selected)
    inspected_set = set(inspected)
    missed = sorted(dataset.must_select - selected_set)
    distractors = sorted(dataset.distractors & selected_set)
    uninspected = sorted(selected_set - inspected_set)
    recall = 1.0 - (len(missed) / len(dataset.must_select))
    content_levels: dict[str, str] = {}
    for event in events:
        if event.content_level:
            for pmid in event.result_pmids:
                content_levels[pmid] = event.content_level
    passed = error is None and not missed and not distractors and not uninspected
    return TrialReport(
        mode=mode,
        run=run,
        seed=seed,
        passed=passed,
        must_select_recall=recall,
        selected_pmids=selected,
        inspected_pmids=inspected,
        surfaced_pmids=surfaced,
        missed_must_select=missed,
        selected_supporting=sorted(dataset.supporting & selected_set),
        selected_distractors=distractors,
        cited_without_inspection=uninspected,
        content_levels=content_levels,
        tool_events=events,
        final_answer=final_answer,
        latency_ms=latency_ms,
        usage=usage,
        error=error,
    )


async def run_trial(
    *,
    mode: str,
    run_number: int,
    seed: int,
    dataset: ResearchDataset,
    prompt: str,
    tools: ResearchTools,
    model_url: str,
    model: str,
    max_turns: int,
    max_tokens: int,
) -> TrialReport:
    scope = (
        "\n\nBENCHMARK SCOPE: Only PubMed research tools are available. Use pubmed_search "
        "to identify candidate papers, inspect every paper you cite with article_find or "
        "article_read, and cite selected papers with their PubMed URLs."
    )
    messages: list[dict] = [
        {"role": "system", "content": prompt + scope},
        {"role": "user", "content": dataset.question},
    ]
    events: list[ToolEvent] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    final_answer = ""
    error = None
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            for _turn in range(max_turns):
                response = await client.post(
                    model_url.rstrip("/") + "/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": TOOL_SCHEMAS,
                        "tool_choice": "auto",
                        "temperature": 0,
                        "seed": seed,
                        "max_tokens": max_tokens,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                for key in usage:
                    usage[key] += int(payload.get("usage", {}).get(key, 0) or 0)
                message = payload["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    final_answer = str(message.get("content") or "")
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    name = str(call.get("function", {}).get("name", ""))
                    raw_arguments = call.get("function", {}).get("arguments", "{}")
                    try:
                        arguments = json.loads(raw_arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be an object")
                        result = await tools.call(name, arguments)
                    except (ValueError, TypeError, json.JSONDecodeError) as exc:
                        arguments = {"_raw": raw_arguments}
                        result = ToolResult(
                            rendered=f"Invalid tool arguments: {exc}", error="invalid_arguments"
                        )
                    events.append(
                        ToolEvent(
                            name=name,
                            arguments=arguments,
                            result_pmids=result.result_pmids,
                            content_level=result.content_level,
                            error=result.error,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": result.rendered,
                        }
                    )
            else:
                error = f"model exceeded {max_turns} tool turns"
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - started) * 1000
    return _score_trial(
        mode=mode,
        run=run_number,
        seed=seed,
        dataset=dataset,
        final_answer=final_answer,
        events=events,
        latency_ms=latency_ms,
        usage=usage,
        error=error,
    )


def _mode_summary(trials: list[TrialReport], dataset: ResearchDataset) -> dict:
    frequencies = Counter(pmid for trial in trials for pmid in set(trial.selected_pmids))
    inspected = Counter(pmid for trial in trials for pmid in set(trial.inspected_pmids))
    return {
        "runs": len(trials),
        "passes": sum(trial.passed for trial in trials),
        "all_passed": bool(trials) and all(trial.passed for trial in trials),
        "mean_must_select_recall": (
            sum(trial.must_select_recall for trial in trials) / len(trials) if trials else 0.0
        ),
        "selection_frequency": {
            pmid: frequencies.get(pmid, 0) / len(trials) for pmid in dataset.candidate_pmids
        },
        "inspection_frequency": {
            pmid: inspected.get(pmid, 0) / len(trials) for pmid in dataset.candidate_pmids
        },
        "mean_latency_ms": (
            sum(trial.latency_ms for trial in trials) / len(trials) if trials else 0.0
        ),
        "mean_tool_calls": (
            sum(len(trial.tool_events) for trial in trials) / len(trials) if trials else 0.0
        ),
        "mean_total_tokens": (
            sum(trial.usage.get("total_tokens", 0) for trial in trials) / len(trials)
            if trials
            else 0.0
        ),
        "error_rate": sum(bool(trial.error) for trial in trials) / len(trials) if trials else 0.0,
    }


def _git_metadata() -> dict[str, object]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=_ROOT, capture_output=True, text=True, check=False
        )
        return result.stdout.strip()

    return {"revision": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


async def model_identity(model_url: str, requested: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(model_url.rstrip("/") + "/models")
            response.raise_for_status()
            models = response.json().get("data", [])
        selected = next((item for item in models if item.get("id") == requested), None)
        reported = selected or {"available_ids": [item.get("id") for item in models]}
        return {"requested": requested, "reported": reported}
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return {"requested": requested, "lookup_error": f"{type(exc).__name__}: {exc}"}


async def run_evaluation(
    *,
    dataset: ResearchDataset,
    prompt: str,
    mode: str,
    repeats: int,
    model_url: str,
    model: str,
    gateway_url: str,
    api_key: str,
    max_turns: int,
    max_tokens: int,
) -> dict:
    snapshot = load_snapshot(dataset)
    effective_prompt = prompt + (
        "\n\nBENCHMARK SCOPE: Only PubMed research tools are available. Use pubmed_search "
        "to identify candidate papers, inspect every paper you cite with article_find or "
        "article_read, and cite selected papers with their PubMed URLs."
    )
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": dataset.name,
        "question": dataset.question,
        "judgments": [asdict(item) for item in dataset.judgments],
        "metadata": {
            "dataset_hash": stable_hash(
                {
                    "version": dataset.version,
                    "name": dataset.name,
                    "question": dataset.question,
                    "candidate_pmids": dataset.candidate_pmids,
                    "judgments": [asdict(item) for item in dataset.judgments],
                }
            ),
            "snapshot_hash": stable_hash(snapshot),
            "prompt_hash": _sha256(effective_prompt),
            "tool_schema_hash": stable_hash(TOOL_SCHEMAS),
            "model": await model_identity(model_url, model),
            "git": _git_metadata(),
            "parameters": {
                "temperature": 0,
                "repeats": repeats,
                "max_turns": max_turns,
                "max_tokens": max_tokens,
            },
        },
        "modes": {},
    }
    modes = [mode] if mode != "both" else ["snapshot", "live"]
    for current_mode in modes:
        tool_client: ResearchTools = (
            SnapshotTools(dataset, snapshot)
            if current_mode == "snapshot"
            else LiveTools(gateway_url, api_key)
        )
        trials = []
        for index in range(repeats):
            trials.append(
                await run_trial(
                    mode=current_mode,
                    run_number=index + 1,
                    seed=41 + index,
                    dataset=dataset,
                    prompt=prompt,
                    tools=tool_client,
                    model_url=model_url,
                    model=model,
                    max_turns=max_turns,
                    max_tokens=max_tokens,
                )
            )
        report["modes"][current_mode] = {
            "summary": _mode_summary(trials, dataset),
            "trials": [asdict(trial) for trial in trials],
        }
    return report


async def capture_snapshot(dataset: ResearchDataset, gateway_url: str, api_key: str) -> dict:
    query = " OR ".join(f"{pmid}[pmid]" for pmid in dataset.candidate_pmids)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            gateway_url.rstrip("/") + "/api/v1/pubmed/search",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "limit": 20},
        )
        response.raise_for_status()
    search = SearchResponse.model_validate(response.json())
    by_pmid = {
        item.article_id.removeprefix("pubmed:"): item
        for item in search.results
        if item.article_id and item.article_id.startswith("pubmed:")
    }
    missing = set(dataset.candidate_pmids) - set(by_pmid)
    if missing:
        raise ResearchDatasetError(f"PubMed omitted snapshot candidates: {sorted(missing)}")
    ordered = [by_pmid[pmid] for pmid in dataset.candidate_pmids]
    search = search.model_copy(update={"results": ordered})
    articles = {}
    for pmid in dataset.candidate_pmids:
        item = by_pmid[pmid]
        abstract = item.abstract or "No abstract was available in the captured PubMed record."
        articles[pmid] = {
            "title": item.title,
            "citation": item.citation,
            "content_level": "abstract" if item.abstract else "metadata",
            "extraction_method": "pubmed_xml_snapshot",
            "text": f"# {item.title}\n\n## Abstract\n\n{abstract}",
        }
    return {
        "version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "canonical_query": query,
        "search_response": search.model_dump(mode="json"),
        "articles": articles,
    }


def format_markdown(report: dict) -> str:
    lines = [
        f"# Research selection evaluation: {report['dataset']}",
        "",
        f"**Question:** {report['question']}",
        "",
        "## Gold judgments",
        "",
        "| PMID | Label | Rationale |",
        "| --- | --- | --- |",
    ]
    for item in report["judgments"]:
        lines.append(f"| {item['pmid']} | {item['label']} | {item['rationale']} |")
    for mode, result in report["modes"].items():
        summary = result["summary"]
        lines += [
            "",
            f"## {mode.title()} findings",
            "",
            f"Passes: **{summary['passes']}/{summary['runs']}**; "
            f"mean must-select recall: **{summary['mean_must_select_recall']:.2f}**.",
            f"Mean latency: **{summary['mean_latency_ms'] / 1000:.1f}s**; "
            f"mean tool calls: **{summary['mean_tool_calls']:.1f}**; "
            f"mean tokens: **{summary['mean_total_tokens']:.0f}**.",
            "",
            (
                "| Run | Pass | Selected | Inspected | Searches (empty) | Latency | "
                "Tokens | Missed gold | Distractors | Error |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for trial in result["trials"]:
            values = {
                key: ", ".join(trial[key]) or "—"
                for key in (
                    "selected_pmids",
                    "inspected_pmids",
                    "missed_must_select",
                    "selected_distractors",
                )
            }
            search_events = [
                event for event in trial["tool_events"] if event["name"] == "pubmed_search"
            ]
            empty_searches = sum(
                not event["result_pmids"] and not event["error"] for event in search_events
            )
            lines.append(
                f"| {trial['run']} | {'yes' if trial['passed'] else 'no'} | "
                f"{values['selected_pmids']} | {values['inspected_pmids']} | "
                f"{len(search_events)} ({empty_searches}) | "
                f"{trial['latency_ms'] / 1000:.1f}s | {trial['usage'].get('total_tokens', 0)} | "
                f"{values['missed_must_select']} | {values['selected_distractors']} | "
                f"{trial['error'] or '—'} |"
            )
        lines += ["", "### Per-document selection frequency", ""]
        for pmid, frequency in summary["selection_frequency"].items():
            if frequency:
                lines.append(f"- PMID {pmid}: {frequency:.0%}")
    comparison = report.get("baseline_comparison")
    if comparison:
        lines += ["", "## Baseline comparison", ""]
        if comparison["compatible"]:
            lines.append(
                f"Prompt changed: **{comparison['prompt_changed']}**; "
                f"tool schema changed: **{comparison['tool_schema_changed']}**."
            )
            for mode, deltas in comparison["selection_frequency_delta"].items():
                changed = {pmid: value for pmid, value in deltas.items() if value}
                if changed:
                    lines.append("")
                    lines.append(f"{mode.title()} selection-frequency changes:")
                    for pmid, value in changed.items():
                        lines.append(f"- PMID {pmid}: {value:+.0%}")
        else:
            lines.append("Incompatible baseline: " + "; ".join(comparison["reasons"]))
    return "\n".join(lines) + "\n"


def compare_baseline(report: dict, baseline: dict) -> dict:
    reasons = []
    if baseline.get("schema_version") != 1:
        reasons.append("baseline schema differs")
    if baseline.get("dataset") != report["dataset"]:
        reasons.append("dataset name differs")
    current_meta = report["metadata"]
    base_meta = baseline.get("metadata", {})
    for key in ("dataset_hash", "snapshot_hash"):
        if base_meta.get(key) != current_meta.get(key):
            reasons.append(f"{key} differs")
    base_model = base_meta.get("model", {}).get("requested")
    current_model = current_meta.get("model", {}).get("requested")
    if base_model != current_model:
        reasons.append("model differs")
    deltas = {}
    if not reasons:
        for mode, value in report["modes"].items():
            old = baseline.get("summaries", {}).get(mode, {}).get("selection_frequency", {})
            current = value["summary"]["selection_frequency"]
            deltas[mode] = {pmid: current[pmid] - float(old.get(pmid, 0.0)) for pmid in current}
    return {
        "compatible": not reasons,
        "reasons": reasons,
        "prompt_changed": base_meta.get("prompt_hash") != current_meta.get("prompt_hash"),
        "tool_schema_changed": (
            base_meta.get("tool_schema_hash") != current_meta.get("tool_schema_hash")
        ),
        "selection_frequency_delta": deltas,
    }


def rescore_report(report: dict, dataset: ResearchDataset) -> dict:
    """Recompute selection metrics from preserved answers and tool metadata."""
    report.pop("baseline_comparison", None)
    report["rescored_at"] = datetime.now(UTC).isoformat()
    for mode, value in report.get("modes", {}).items():
        trials = []
        for raw in value.get("trials", []):
            events = [ToolEvent(**event) for event in raw.get("tool_events", [])]
            trials.append(
                _score_trial(
                    mode=mode,
                    run=int(raw["run"]),
                    seed=int(raw["seed"]),
                    dataset=dataset,
                    final_answer=str(raw.get("final_answer", "")),
                    events=events,
                    latency_ms=float(raw.get("latency_ms", 0.0)),
                    usage=dict(raw.get("usage", {})),
                    error=raw.get("error"),
                )
            )
        value["trials"] = [asdict(trial) for trial in trials]
        value["summary"] = _mode_summary(trials, dataset)
    return report


def _read_key(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"gateway key file does not exist: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit(f"gateway key file is empty: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--mode", choices=("snapshot", "live", "both"), default="both")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model-url", default=DEFAULT_MODEL_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    parser.add_argument("--gateway-key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--capture-snapshot", action="store_true")
    parser.add_argument(
        "--rescore-report",
        type=Path,
        help="recompute metrics from an existing JSON report without rerunning the model",
    )
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 20:
        raise SystemExit("--repeats must be in 1..20")
    if args.max_turns < 1 or args.max_turns > 50:
        raise SystemExit("--max-turns must be in 1..50")

    dataset = load_research_dataset(args.dataset)
    if args.rescore_report:
        report = json.loads(args.rescore_report.read_text(encoding="utf-8"))
        report = rescore_report(report, dataset)
        markdown = format_markdown(report)
        target_json = args.json or args.rescore_report
        _write_json(target_json, report)
        if args.markdown:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(markdown, encoding="utf-8")
        print(markdown)
        if args.update_baseline:
            _write_json(
                args.baseline,
                {
                    "schema_version": 1,
                    "dataset": report["dataset"],
                    "metadata": report["metadata"],
                    "summaries": {
                        mode: value["summary"] for mode, value in report["modes"].items()
                    },
                },
            )
            print(f"baseline written to {args.baseline}")
        return
    api_key = (
        _read_key(args.gateway_key_file)
        if args.capture_snapshot or args.mode in {"live", "both"}
        else ""
    )
    if args.capture_snapshot:
        snapshot = asyncio.run(capture_snapshot(dataset, args.gateway_url, api_key))
        _write_json(dataset.snapshot_path, snapshot)
        print(f"snapshot written to {dataset.snapshot_path}")
        return

    prompt = load_research_prompt(args.prompt)
    report = asyncio.run(
        run_evaluation(
            dataset=dataset,
            prompt=prompt,
            mode=args.mode,
            repeats=args.repeats,
            model_url=args.model_url,
            model=args.model,
            gateway_url=args.gateway_url,
            api_key=api_key,
            max_turns=args.max_turns,
            max_tokens=args.max_tokens,
        )
    )
    if args.baseline.exists():
        stored_baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report["baseline_comparison"] = compare_baseline(report, stored_baseline)
    markdown = format_markdown(report)
    print(markdown)
    if args.json:
        _write_json(args.json, report)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown, encoding="utf-8")
    if args.update_baseline:
        _write_json(
            args.baseline,
            {
                "schema_version": 1,
                "dataset": report["dataset"],
                "metadata": report["metadata"],
                "summaries": {
                    mode: value["summary"] for mode, value in report["modes"].items()
                },
            },
        )
        print(f"baseline written to {args.baseline}")
    if args.check:
        snapshot_result = report["modes"].get("snapshot")
        if not snapshot_result:
            raise SystemExit("--check requires snapshot or both mode")
        comparison = report.get("baseline_comparison")
        if comparison and not comparison["compatible"]:
            raise SystemExit(
                "research selection baseline is incompatible: "
                + "; ".join(comparison["reasons"])
            )
        if not snapshot_result["summary"]["all_passed"]:
            raise SystemExit("research selection gate failed")
        print("research selection gate passed")


if __name__ == "__main__":
    main()
