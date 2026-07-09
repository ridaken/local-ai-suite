"""Tests for OPDS v2 catalog feed parsing. The HTTP-calling wrapper
(search_catalog) isn't unit tested, matching this repo's convention for other
live-network tools (see test_compute.py) — only the pure parsing logic is.
"""

from mcp_gateway.catalog import parse_catalog_feed

# Trimmed real response from library.kiwix.org/catalog/v2/entries, kept close to
# the wire format (including the .meta4 acquisition link and _ftindex tag) so a
# schema change upstream would break this test rather than production silently.
_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/terms/">
  <id>0309255d-575f-560c-61d3-10f3c53df0e6</id>
  <title>Filtered Entries</title>
  <updated>2026-07-09T17:03:08Z</updated>
  <totalResults>2</totalResults>
  <entry>
    <id>urn:uuid:9528fea1-132e-9621-e133-e8a6c200e8c7</id>
    <title>Stack Overflow</title>
    <updated>2026-02-04T00:00:00Z</updated>
    <summary>Q&amp;A for professional programmers</summary>
    <language>eng</language>
    <name>stackoverflow.com_en_all</name>
    <flavour></flavour>
    <category>stack_exchange</category>
    <tags>stack_exchange;_category:stack_exchange;_ftindex:yes;_pictures:yes</tags>
    <articleCount>46764</articleCount>
    <mediaCount>266</mediaCount>
    <link type="text/html"
          href="https://browse.library.kiwix.org/content/stackoverflow.com_en_all" />
    <dc:issued>2026-02-04T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://lb.download.kiwix.org/zim/stack_exchange/stackoverflow.com_en_all_2026-02.zim.meta4"
          length="236789760" />
  </entry>
  <entry>
    <id>urn:uuid:600c47c6-a792-0f78-8fcc-9c05cf1c556b</id>
    <title>Wikipedia (mini)</title>
    <updated>2024-08-13T00:00:00Z</updated>
    <summary>No full-text search</summary>
    <language>eng</language>
    <name>wikipedia_en_all_mini</name>
    <flavour>mini</flavour>
    <category>wikipedia</category>
    <tags>_category:wikipedia;_ftindex:no</tags>
    <articleCount>11604</articleCount>
    <mediaCount>0</mediaCount>
  </entry>
</feed>
"""


def test_parse_catalog_feed_returns_all_entries():
    entries = parse_catalog_feed(_FEED)
    assert len(entries) == 2


def test_parse_catalog_feed_extracts_fields():
    entries = parse_catalog_feed(_FEED)
    so = entries[0]
    assert so.uuid == "9528fea1-132e-9621-e133-e8a6c200e8c7"
    assert so.name == "stackoverflow.com_en_all"
    assert so.title == "Stack Overflow"
    assert so.language == "eng"
    assert so.article_count == 46764
    assert so.media_count == 266


def test_parse_catalog_feed_strips_meta4_suffix_for_download_url():
    entries = parse_catalog_feed(_FEED)
    so = entries[0]
    assert so.download_url == (
        "https://lb.download.kiwix.org/zim/stack_exchange/stackoverflow.com_en_all_2026-02.zim"
    )
    assert so.size_bytes == 236789760


def test_parse_catalog_feed_reads_fulltext_index_flag():
    entries = parse_catalog_feed(_FEED)
    assert entries[0].has_fulltext_index is True
    assert entries[1].has_fulltext_index is False


def test_parse_catalog_feed_handles_missing_acquisition_link():
    entries = parse_catalog_feed(_FEED)
    mini = entries[1]
    assert mini.download_url is None
    assert mini.size_bytes is None


def test_parse_catalog_feed_empty_feed():
    empty = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><title>Empty</title></feed>"""
    assert parse_catalog_feed(empty) == []


def test_parse_catalog_feed_malformed_xml_returns_empty_list():
    assert parse_catalog_feed(b"<html>upstream error") == []
