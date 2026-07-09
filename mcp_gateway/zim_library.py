"""ZIM library management: scan installed ZIM files, read their metadata, and
generate the library.xml kiwix-serve consumes in `--library --monitorLibrary`
mode. This is what lets the admin UI add/remove books without restarting the
kiwix container — kiwix watches library.xml and reloads it on change.

library.xml schema (verified against libkiwix's Book::updateFromXml): the
<book> element's `name` attribute is what kiwix's search API filters on via
`books.name=`, so it doubles as the identifier settings_store/lexical.py use
for per-book toggles.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


@dataclass
class BookInfo:
    filename: str  # e.g. "wikipedia_en_all_nopic_2026-01.zim"
    name: str  # ZIM "Name" metadata; falls back to filename stem. Used for books.name= filtering.
    uuid: str
    title: str = ""
    description: str = ""
    language: str = ""
    creator: str = ""
    publisher: str = ""
    date: str = ""
    tags: str = ""
    flavour: str = ""
    article_count: int = 0
    media_count: int = 0
    size_bytes: int = 0
    has_fulltext_index: bool = False
    metadata_error: str | None = field(default=None)


def _read_metadata(archive, key: str) -> str:
    try:
        return archive.get_metadata(key).decode("utf-8", errors="replace")
    except (KeyError, RuntimeError):
        return ""


def read_book_info(zim_path: Path) -> BookInfo:
    """Open a ZIM and extract the fields library.xml + the admin UI need.

    Raises whatever python-libzim raises on a corrupt/unreadable file — callers
    (scan_zim_dir) catch per-file so one bad ZIM doesn't break the whole listing.
    """
    from libzim.reader import Archive

    archive = Archive(str(zim_path))
    name = _read_metadata(archive, "Name") or zim_path.stem
    return BookInfo(
        filename=zim_path.name,
        name=name,
        uuid=str(archive.uuid),
        title=_read_metadata(archive, "Title") or name,
        description=_read_metadata(archive, "Description"),
        language=_read_metadata(archive, "Language"),
        creator=_read_metadata(archive, "Creator"),
        publisher=_read_metadata(archive, "Publisher"),
        date=_read_metadata(archive, "Date"),
        tags=_read_metadata(archive, "Tags"),
        flavour=_read_metadata(archive, "Flavour"),
        article_count=archive.article_count,
        media_count=archive.media_count,
        size_bytes=archive.filesize,
        has_fulltext_index=archive.has_fulltext_index,
    )


def scan_zim_dir(zim_dir: Path) -> list[BookInfo]:
    """List every *.zim in zim_dir with its metadata. Unreadable files are
    included with metadata_error set rather than dropped, so the admin UI can
    surface "this ZIM looks broken" instead of silently hiding it."""
    zim_dir = Path(zim_dir)
    if not zim_dir.is_dir():
        return []
    books: list[BookInfo] = []
    for path in sorted(zim_dir.glob("*.zim")):
        try:
            books.append(read_book_info(path))
        except Exception as exc:  # noqa: BLE001 - one bad ZIM shouldn't break the listing
            books.append(
                BookInfo(
                    filename=path.name,
                    name=path.stem,
                    uuid="",
                    title=path.stem,
                    metadata_error=f"{type(exc).__name__}: {exc}",
                )
            )
    return books


def render_library_xml(books: list[BookInfo], container_data_dir: str) -> str:
    """Render the library.xml document kiwix-serve loads in --library mode.
    Books with a metadata_error are skipped — kiwix couldn't read them either."""
    root = ET.Element("library", version="20110515")
    for book in books:
        if book.metadata_error:
            continue
        container_path = f"{container_data_dir.rstrip('/')}/{book.filename}"
        attrs = {
            "id": book.uuid,
            "path": container_path,
            "name": book.name,
            "title": book.title,
            "description": book.description,
            "language": book.language,
            "creator": book.creator,
            "publisher": book.publisher,
            "date": book.date,
            "tags": book.tags,
            "flavour": book.flavour,
            "articleCount": str(book.article_count),
            "mediaCount": str(book.media_count),
            "size": str(book.size_bytes // 1024),  # libkiwix stores size in KiB
        }
        ET.SubElement(root, "book", {k: v for k, v in attrs.items() if v})
    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}\n'


def write_library_xml(books: list[BookInfo], dest: Path) -> None:
    """Atomic write so kiwix's --monitorLibrary never observes a half-written file."""
    from . import config

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)  # ZIM_DIR may not exist yet on a fresh setup
    xml = render_library_xml(books, config.KIWIX_DATA_DIR)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(xml, encoding="utf-8")
    os.replace(tmp, dest)


def refresh_library(zim_dir: Path, library_path: Path) -> list[BookInfo]:
    """Rescan zim_dir and rewrite library.xml. Call after any download/delete."""
    books = scan_zim_dir(zim_dir)
    write_library_xml(books, library_path)
    return books


def installed_book_names(library_path: Path) -> list[str]:
    """Read the `name` of every book in an already-generated library.xml.

    Used by retrieval to know which book names exist before asking
    settings_store which of them are enabled — cheap (just parses the XML this
    module already wrote) rather than re-opening every ZIM via libzim on each
    kb_search call. Returns [] if library.xml doesn't exist yet (Phase 3 not
    set up, or nothing downloaded) so callers can fall back to unfiltered search.
    """
    library_path = Path(library_path)
    if not library_path.is_file():
        return []
    try:
        root = ET.parse(library_path).getroot()
    except ET.ParseError:
        return []
    return [book.get("name", "") for book in root.findall("book") if book.get("name")]
