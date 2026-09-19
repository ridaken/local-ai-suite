"""Validated datasets for end-to-end research document-selection evaluations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
_PMID = re.compile(r"^\d{6,12}$")


class ResearchDatasetError(ValueError):
    """The research evaluation dataset or its snapshot is malformed."""


@dataclass(frozen=True)
class Judgment:
    pmid: str
    label: str
    rationale: str


@dataclass(frozen=True)
class ResearchDataset:
    version: int
    name: str
    question: str
    candidate_pmids: tuple[str, ...]
    judgments: tuple[Judgment, ...]
    snapshot_path: Path

    @property
    def must_select(self) -> frozenset[str]:
        return frozenset(j.pmid for j in self.judgments if j.label == "must_select")

    @property
    def supporting(self) -> frozenset[str]:
        return frozenset(j.pmid for j in self.judgments if j.label == "supporting")

    @property
    def distractors(self) -> frozenset[str]:
        return frozenset(j.pmid for j in self.judgments if j.label == "distractor")


def _nonempty(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ResearchDatasetError(f"{field} must be a non-empty string")
    return text


def load_research_dataset(path: Path) -> ResearchDataset:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ResearchDatasetError("dataset must be a mapping")
    if raw.get("version") != SCHEMA_VERSION:
        raise ResearchDatasetError(
            f"dataset version {raw.get('version')!r} is not supported "
            f"(expected {SCHEMA_VERSION})"
        )

    candidates = raw.get("candidate_pmids")
    if not isinstance(candidates, list) or not candidates:
        raise ResearchDatasetError("candidate_pmids must be a non-empty list")
    pmids = tuple(str(value).strip() for value in candidates)
    if any(not _PMID.fullmatch(pmid) for pmid in pmids):
        raise ResearchDatasetError("candidate_pmids must contain 6-12 digit PubMed IDs")
    if len(set(pmids)) != len(pmids):
        raise ResearchDatasetError("candidate_pmids contains duplicates")

    raw_judgments = raw.get("judgments")
    if not isinstance(raw_judgments, list) or not raw_judgments:
        raise ResearchDatasetError("judgments must be a non-empty list")
    judgments: list[Judgment] = []
    seen: set[str] = set()
    labels = {"must_select", "supporting", "distractor"}
    for index, item in enumerate(raw_judgments, start=1):
        if not isinstance(item, dict):
            raise ResearchDatasetError(f"judgment #{index} must be a mapping")
        pmid = str(item.get("pmid", "")).strip()
        label = str(item.get("label", "")).strip()
        if pmid not in pmids:
            raise ResearchDatasetError(f"judgment PMID {pmid!r} is not a candidate")
        if pmid in seen:
            raise ResearchDatasetError(f"duplicate judgment for PMID {pmid}")
        if label not in labels:
            raise ResearchDatasetError(f"judgment PMID {pmid}: invalid label {label!r}")
        seen.add(pmid)
        judgments.append(
            Judgment(
                pmid=pmid,
                label=label,
                rationale=_nonempty(item.get("rationale"), "rationale"),
            )
        )
    missing = set(pmids) - seen
    if missing:
        raise ResearchDatasetError(f"candidates without judgments: {sorted(missing)}")
    if not any(item.label == "must_select" for item in judgments):
        raise ResearchDatasetError("at least one must_select judgment is required")

    snapshot_value = _nonempty(raw.get("snapshot"), "snapshot")
    snapshot_path = (path.parent / snapshot_value).resolve()
    return ResearchDataset(
        version=SCHEMA_VERSION,
        name=_nonempty(raw.get("name"), "name"),
        question=_nonempty(raw.get("question"), "question"),
        candidate_pmids=pmids,
        judgments=tuple(judgments),
        snapshot_path=snapshot_path,
    )


def load_snapshot(dataset: ResearchDataset) -> dict:
    if not dataset.snapshot_path.exists():
        raise ResearchDatasetError(f"snapshot does not exist: {dataset.snapshot_path}")
    raw = json.loads(dataset.snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ResearchDatasetError("snapshot must be a version 1 JSON object")
    response = raw.get("search_response")
    articles = raw.get("articles")
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise ResearchDatasetError("snapshot search_response is malformed")
    if not isinstance(articles, dict):
        raise ResearchDatasetError("snapshot articles must be a mapping")
    result_pmids = {
        str(item.get("article_id", "")).removeprefix("pubmed:")
        for item in response["results"]
        if isinstance(item, dict)
    }
    expected = set(dataset.candidate_pmids)
    if result_pmids != expected:
        missing = sorted(expected - result_pmids)
        extra = sorted(result_pmids - expected)
        raise ResearchDatasetError(
            f"snapshot result PMIDs differ from dataset: missing={missing}, extra={extra}"
        )
    if set(articles) != expected:
        raise ResearchDatasetError("snapshot article PMIDs differ from dataset")
    return raw


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
