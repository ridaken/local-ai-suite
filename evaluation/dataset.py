"""Versioned evaluation dataset.

The dataset is the contract for "did retrieval get worse", so it is validated
strictly and versioned explicitly: a silently malformed case that scores 1.0 by
default would hide the regression it was written to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Bump when the case schema changes incompatibly. Reports record the version they
# ran against so two numbers are never compared across schemas.
SCHEMA_VERSION = 1


class DatasetError(ValueError):
    """The dataset file is malformed."""


@dataclass
class Case:
    id: str
    query: str
    #  Citations retrieval is expected to return for this query.
    expected_sources: list[str] = field(default_factory=list)
    # Text that an answer-bearing passage must contain. Used for citation
    # correctness: a cited source should carry the answer, not just be relevant.
    answer_passages: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Dataset:
    version: int
    name: str
    cases: list[Case]


def _string_list(entry: dict, key: str, case_id: str) -> list[str]:
    value = entry.get(key, [])
    if isinstance(value, str) or not isinstance(value, list):
        raise DatasetError(f"case {case_id!r}: {key} must be a list of strings")
    if not all(isinstance(v, str) and v.strip() for v in value):
        raise DatasetError(f"case {case_id!r}: {key} must contain non-empty strings")
    return [v.strip() for v in value]


def load_dataset(path: Path) -> Dataset:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise DatasetError("dataset must be a mapping")

    version = raw.get("version")
    if version != SCHEMA_VERSION:
        raise DatasetError(
            f"dataset version {version!r} is not supported (expected {SCHEMA_VERSION})"
        )
    entries = raw.get("cases")
    if not isinstance(entries, list) or not entries:
        raise DatasetError("dataset must define a non-empty 'cases' list")

    cases: list[Case] = []
    seen: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise DatasetError(f"case #{position} must be a mapping")
        case_id = str(entry.get("id", "")).strip()
        if not case_id:
            raise DatasetError(f"case #{position} is missing an 'id'")
        if case_id in seen:
            raise DatasetError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        query = str(entry.get("query", "")).strip()
        if not query:
            raise DatasetError(f"case {case_id!r} is missing a 'query'")
        expected = _string_list(entry, "expected_sources", case_id)
        if not expected:
            raise DatasetError(
                f"case {case_id!r} lists no expected_sources — a case that expects "
                "nothing always passes and measures nothing"
            )
        cases.append(
            Case(
                id=case_id,
                query=query,
                expected_sources=expected,
                answer_passages=_string_list(entry, "answer_passages", case_id),
                notes=str(entry.get("notes", "")),
            )
        )
    return Dataset(version=version, name=str(raw.get("name", path.stem)), cases=cases)
