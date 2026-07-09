"""Client for the Kiwix OPDS v2 catalog (library.kiwix.org/catalog/v2/entries) —
lets the admin UI browse/search ZIMs available to download without hand-editing
a URL.

The catalog's acquisition link points at a `.zim.meta4` Metalink wrapper, not
the ZIM itself; stripping the `.meta4` suffix yields the direct file URL (the
same pattern as the download.kiwix.org mirror layout), which avoids adding a
Metalink XML parser for one field.

The feed's `tags` also encode `_ftindex:yes|no` — whether the ZIM ships a
full-text index. kb_search depends on that index, so the catalog UI surfaces it
to steer people away from `mini`-flavour ZIMs that lack it.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from . import config

_NS = {"atom": "http://www.w3.org/2005/Atom"}
_ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"


@dataclass
class CatalogEntry:
    uuid: str
    name: str
    title: str
    description: str
    language: str
    category: str
    tags: str
    article_count: int
    media_count: int
    updated: str
    size_bytes: int | None
    download_url: str | None
    has_fulltext_index: bool | None


def _int_or_zero(text: str | None) -> int:
    try:
        return int(text or "0")
    except ValueError:
        return 0


def _fulltext_flag(tags: str) -> bool | None:
    for part in tags.split(";"):
        if part.strip() == "_ftindex:yes":
            return True
        if part.strip() == "_ftindex:no":
            return False
    return None


def _download_url(link_href: str) -> str:
    return link_href[: -len(".meta4")] if link_href.endswith(".meta4") else link_href


def _parse_entry(entry: ET.Element) -> CatalogEntry:
    def text(tag: str) -> str:
        return (entry.findtext(f"atom:{tag}", default="", namespaces=_NS) or "").strip()

    size_bytes: int | None = None
    download_url: str | None = None
    for link in entry.findall("atom:link", _NS):
        if link.get("rel") == _ACQUISITION_REL:
            href = link.get("href", "")
            download_url = _download_url(href) if href else None
            length = link.get("length")
            size_bytes = int(length) if length and length.isdigit() else None
            break

    tags = text("tags")
    return CatalogEntry(
        uuid=text("id").removeprefix("urn:uuid:"),
        name=text("name"),
        title=text("title"),
        description=text("summary"),
        language=text("language"),
        category=text("category"),
        tags=tags,
        article_count=_int_or_zero(text("articleCount")),
        media_count=_int_or_zero(text("mediaCount")),
        updated=text("updated"),
        size_bytes=size_bytes,
        download_url=download_url,
        has_fulltext_index=_fulltext_flag(tags),
    )


def parse_catalog_feed(xml_bytes: bytes) -> list[CatalogEntry]:
    root = ET.fromstring(xml_bytes)
    return [_parse_entry(entry) for entry in root.findall("atom:entry", _NS)]


async def search_catalog(query: str = "", lang: str = "", count: int = 30) -> list[CatalogEntry]:
    """Query the OPDS catalog. Raises httpx.HTTPError if unreachable."""
    params: dict[str, str] = {"count": str(count)}
    if query:
        params["q"] = query
    if lang:
        params["lang"] = lang

    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(
            config.KIWIX_CATALOG_URL,
            params=params,
            headers={"User-Agent": config.USER_AGENT},
        )
    resp.raise_for_status()
    return parse_catalog_feed(resp.content)
