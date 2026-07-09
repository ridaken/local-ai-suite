"""Tests for ZIM metadata reading and library.xml generation.

read_book_info/scan_zim_dir are tested against a fake libzim.reader.Archive
(monkeypatched at the module attribute kiwix_library imports from at call
time) since a real .zim fixture isn't available in CI. render_library_xml /
write_library_xml need no ZIM at all — they operate on BookInfo values.
"""

from pathlib import Path

from mcp_gateway.zim_library import (
    BookInfo,
    read_book_info,
    render_library_xml,
    scan_zim_dir,
    write_library_xml,
)


class _FakeArchive:
    def __init__(self, path: str, metadata: dict[str, str] | None = None, **attrs):
        self.path = path
        self._metadata = metadata or {}
        self.uuid = attrs.get("uuid", "fake-uuid-1234")
        self.article_count = attrs.get("article_count", 100)
        self.media_count = attrs.get("media_count", 10)
        self.filesize = attrs.get("filesize", 2048)
        self.has_fulltext_index = attrs.get("has_fulltext_index", True)

    def get_metadata(self, key: str) -> bytes:
        if key not in self._metadata:
            raise KeyError(key)
        return self._metadata[key].encode("utf-8")


def _patch_archive(monkeypatch, factory):
    import libzim.reader

    monkeypatch.setattr(libzim.reader, "Archive", factory)


def test_read_book_info_extracts_metadata(monkeypatch, tmp_path):
    def factory(path):
        return _FakeArchive(
            path,
            metadata={"Name": "wikipedia_en_all", "Title": "Wikipedia", "Language": "eng"},
        )

    _patch_archive(monkeypatch, factory)
    zim_path = tmp_path / "wikipedia.zim"
    zim_path.write_bytes(b"")  # content unused; Archive is faked

    info = read_book_info(zim_path)
    assert info.name == "wikipedia_en_all"
    assert info.title == "Wikipedia"
    assert info.language == "eng"
    assert info.article_count == 100
    assert info.has_fulltext_index is True
    assert info.metadata_error is None


def test_read_book_info_falls_back_to_filename_stem_when_no_name(monkeypatch, tmp_path):
    _patch_archive(monkeypatch, lambda path: _FakeArchive(path))
    zim_path = tmp_path / "mystery_book.zim"
    zim_path.write_bytes(b"")

    info = read_book_info(zim_path)
    assert info.name == "mystery_book"
    assert info.title == "mystery_book"  # Title also missing -> falls back to name


def test_scan_zim_dir_isolates_a_broken_zim(monkeypatch, tmp_path):
    def factory(path):
        if "broken" in path:
            raise RuntimeError("corrupt zim")
        return _FakeArchive(path, metadata={"Name": Path(path).stem})

    _patch_archive(monkeypatch, factory)
    (tmp_path / "good.zim").write_bytes(b"")
    (tmp_path / "broken.zim").write_bytes(b"")

    books = scan_zim_dir(tmp_path)
    names = {b.filename: b for b in books}
    assert names["good.zim"].metadata_error is None
    assert names["broken.zim"].metadata_error is not None
    assert "corrupt zim" in names["broken.zim"].metadata_error


def test_scan_zim_dir_empty_when_missing(tmp_path):
    assert scan_zim_dir(tmp_path / "does-not-exist") == []


def test_render_library_xml_includes_book_attributes():
    book = BookInfo(
        filename="wikipedia.zim",
        name="wikipedia_en_all",
        uuid="abc-123",
        title="Wikipedia",
        description="An encyclopedia",
        language="eng",
        article_count=42,
        media_count=7,
        size_bytes=2048,
        has_fulltext_index=True,
    )
    xml = render_library_xml([book], container_data_dir="/data")
    assert '<library version="20110515">' in xml
    assert 'id="abc-123"' in xml
    assert 'path="/data/wikipedia.zim"' in xml
    assert 'name="wikipedia_en_all"' in xml
    assert 'title="Wikipedia"' in xml
    assert 'articleCount="42"' in xml
    assert 'size="2"' in xml  # 2048 bytes -> 2 KiB


def test_render_library_xml_skips_books_with_metadata_error():
    broken = BookInfo(filename="broken.zim", name="broken", uuid="", metadata_error="boom")
    xml = render_library_xml([broken], container_data_dir="/data")
    assert "<book" not in xml


def test_write_library_xml_is_atomic(tmp_path):
    book = BookInfo(filename="a.zim", name="a", uuid="u1", title="A", size_bytes=1024)
    dest = tmp_path / "library.xml"
    write_library_xml([book], dest)

    assert dest.exists()
    assert not dest.with_suffix(".xml.tmp").exists()
    content = dest.read_text(encoding="utf-8")
    assert 'name="a"' in content


def test_write_library_xml_creates_missing_parent_dir(tmp_path):
    # ZIM_DIR may not exist yet on a fresh setup (nothing downloaded, no
    # library-init step run) — writing library.xml shouldn't require it to
    # already be there.
    dest = tmp_path / "not-yet-created" / "library.xml"
    write_library_xml([], dest)
    assert dest.exists()
