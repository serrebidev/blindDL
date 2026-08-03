# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import tempfile
import threading
import unittest
from unittest import mock

from blinddl import annas_backend, book_backend


class _Response:
    def __init__(self, payload=None, text="", status_code=200, content=b"",
                 headers=None):
        self._payload = payload
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MatchingTests(unittest.TestCase):
    def test_a_query_naming_the_author_still_matches_the_title(self):
        with_author = book_backend.score_match(
            "moby dick melville", "Moby-Dick; or, The Whale", "Herman Melville")
        unrelated = book_backend.score_match(
            "moby dick melville", "The Red House Mystery", "A. A. Milne")

        self.assertGreater(with_author, 70)
        self.assertLess(unrelated, book_backend.MIN_SCORE)

    def test_ranking_keeps_the_source_order_for_equal_scores(self):
        items = [
            {"title": "Moby Dick", "author": "Herman Melville"},
            {"title": "Moby Dick", "author": "Herman Melville"},
        ]
        first, second = items

        ranked = book_backend._rank(items, "moby dick")

        self.assertIs(ranked[0], first)
        self.assertIs(ranked[1], second)

    def test_ranking_keeps_everything_when_nothing_scores_well(self):
        items = [{"title": "Unrelated", "author": ""}]

        self.assertEqual(len(book_backend._rank(items, "moby dick")), 1)


class ArchiveSourceTests(unittest.TestCase):
    def test_lending_only_items_are_never_offered(self):
        docs = [
            {"identifier": "open", "title": "Open", "creator": "A",
             "format": ["EPUB", "Text PDF"], "year": 1851},
            {"identifier": "lending", "title": "Lending", "creator": "A",
             "format": ["EPUB"], "access-restricted-item": "true"},
            {"identifier": "encrypted", "title": "Encrypted", "creator": "A",
             "format": ["LCP Encrypted EPUB", "ACS Encrypted PDF"]},
        ]
        with mock.patch.object(book_backend, "_ia_query", return_value=docs):
            items = book_backend.search_archive("moby dick")

        self.assertEqual([item["identifier"] for item in items], ["open"])
        self.assertEqual(items[0]["format"], book_backend.FORMAT_EPUB)

    def test_file_choice_prefers_epub_and_refuses_drm(self):
        payload = {
            "metadata": {},
            "files": [
                {"name": "book_lcp.epub", "format": "LCP Encrypted EPUB",
                 "size": "10"},
                {"name": "book.pdf", "format": "Text PDF", "size": "300"},
                {"name": "book.epub", "format": "EPUB", "size": "200"},
                {"name": "book_djvu.txt", "format": "DjVuTXT", "size": "100"},
            ],
        }
        with mock.patch.object(book_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=payload)
            url, name, book_format, size = book_backend.resolve_archive_file(
                "item")

        self.assertEqual(name, "book.epub")
        self.assertEqual(book_format, book_backend.FORMAT_EPUB)
        self.assertEqual(size, 200)
        self.assertTrue(url.endswith("/download/item/book.epub"))

    def test_lending_only_item_refuses_to_resolve(self):
        payload = {"metadata": {"access-restricted-item": "true"},
                   "files": [{"name": "book.epub", "format": "EPUB"}]}
        with mock.patch.object(book_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=payload)
            with self.assertRaises(RuntimeError) as caught:
                book_backend.resolve_archive_file("item")

        self.assertIn("lending-only", str(caught.exception))


class OpenLibraryTests(unittest.TestCase):
    def test_only_public_editions_with_a_scan_are_listed(self):
        payload = {"docs": [
            {"key": "/works/1", "title": "Public", "author_name": ["A"],
             "ebook_access": "public", "ia": ["public_scan"],
             "first_publish_year": 1851},
            {"key": "/works/2", "title": "Borrowable", "author_name": ["A"],
             "ebook_access": "borrowable", "ia": ["lending_scan"]},
            {"key": "/works/3", "title": "No scan", "author_name": ["A"],
             "ebook_access": "public"},
        ]}
        with mock.patch.object(book_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=payload)
            items = book_backend.search_openlibrary("moby dick")

        self.assertEqual([item["title"] for item in items], ["Public"])
        self.assertEqual(items[0]["identifier"], "public_scan")


class GutenbergTests(unittest.TestCase):
    def test_epub_wins_and_zip_archives_are_skipped(self):
        payload = {"results": [{
            "id": 2701,
            "title": "Moby Dick",
            "authors": [{"name": "Melville, Herman"}],
            "formats": {
                "text/plain; charset=utf-8": "https://gutenberg/2701.txt",
                "application/epub+zip": "https://gutenberg/2701.epub3.images",
                "application/zip": "https://gutenberg/2701.zip",
            },
        }]}
        with mock.patch.object(book_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=payload)
            items = book_backend.search_gutenberg("moby dick")

        self.assertEqual(items[0]["format"], book_backend.FORMAT_EPUB)
        self.assertEqual(items[0]["download_url"],
                         "https://gutenberg/2701.epub3.images")


class StandardEbooksTests(unittest.TestCase):
    FEED = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>https://standardebooks.org/ebooks/herman-melville/moby-dick</id>
        <title>Moby Dick</title>
        <author><name>Herman Melville</name></author>
        <published>2018-03-27T22:02:30Z</published>
        <link href="https://se/cover.jpg" rel="http://opds-spec.org/image"
              type="image/jpeg"/>
        <link href="https://se/moby-dick.epub" length="1107183"
              rel="http://opds-spec.org/acquisition/open-access"
              type="application/epub+zip"/>
        <link href="https://se/moby-dick_advanced.epub" length="1401561"
              rel="http://opds-spec.org/acquisition/open-access"
              type="application/epub+zip"/>
      </entry>
    </feed>"""

    def test_the_recommended_epub_is_the_one_offered(self):
        with mock.patch.object(book_backend, "_http") as http:
            http.return_value.get.return_value = _Response(
                content=self.FEED.encode("utf-8"))
            items = book_backend.search_standard_ebooks("moby dick")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["download_url"], "https://se/moby-dick.epub")
        self.assertEqual(items[0]["size_bytes"], 1107183)
        self.assertEqual(items[0]["year"], "2018")


class AnnasArchiveTests(unittest.TestCase):
    ROW = (
        '<a href="/md5/f8e1b8738bc552abe59a5b99e316b19b" class="custom-a '
        'font-semibold text-lg leading-[1.2]">Moby Dick</a>'
        '<a href="/search?q=Herman Melville" class="custom-a">'
        '<span class="icon-[mdi--user-edit] text-base"></span> '
        'Herman Melville</a>'
        '<a href="/search?q=Acheron Press" class="custom-a">'
        '<span class="icon-[mdi--company] text-base"></span> '
        'Acheron Press, 2012</a>'
        '<div class="text-gray-800 font-semibold text-sm leading-[1.2] mt-2">'
        'English [en] · MOBI · 1.7MB · 2012 · '
        '\U0001F680/lgli/upload/zlib · </div>'
    )

    def test_a_result_row_yields_md5_title_author_format_and_size(self):
        rows = annas_backend._parse_rows(self.ROW, "annas-archive.gl")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["md5"], "f8e1b8738bc552abe59a5b99e316b19b")
        self.assertEqual(row["title"], "Moby Dick")
        self.assertEqual(row["author"], "Herman Melville")
        self.assertEqual(row["format"], "MOBI")
        self.assertEqual(row["year"], "2012")
        self.assertAlmostEqual(row["size_bytes"], int(1.7 * 1024 ** 2))
        self.assertTrue(row["on_libgen"])

    def test_records_libgen_can_serve_are_listed_first(self):
        rows = [
            {"md5": "a" * 32, "title": "Zlib only", "author": "",
             "on_libgen": False},
            {"md5": "b" * 32, "title": "On LibGen", "author": "",
             "on_libgen": True},
        ]
        with mock.patch.object(annas_backend, "search", return_value=rows):
            items = book_backend.search_annas("moby dick")

        self.assertEqual([item["title"] for item in items],
                         ["On LibGen", "Zlib only"])

    def test_a_membership_key_is_tried_before_libgen(self):
        with (mock.patch.object(annas_backend, "_member_download_url",
                                return_value="https://fast/book.epub") as member,
              mock.patch.object(annas_backend, "libgen_download_url") as libgen):
            url = annas_backend.resolve_download("a" * 32, member_key="key")

        self.assertEqual(url, "https://fast/book.epub")
        member.assert_called_once()
        libgen.assert_not_called()

    def test_without_a_key_the_libgen_mirrors_answer(self):
        with (mock.patch.object(annas_backend, "_member_download_url") as member,
              mock.patch.object(annas_backend, "libgen_download_url",
                                return_value="https://libgen.li/get.php?x")):
            url = annas_backend.resolve_download("a" * 32)

        self.assertEqual(url, "https://libgen.li/get.php?x")
        member.assert_not_called()

    def test_a_record_no_mirror_carries_explains_the_next_step(self):
        with mock.patch.object(annas_backend, "libgen_download_url",
                               return_value=""):
            with self.assertRaises(RuntimeError) as caught:
                annas_backend.resolve_download("a" * 32)

        self.assertIn("Control C", str(caught.exception))

    def test_libgen_keyed_link_is_pulled_out_of_the_ads_page(self):
        md5 = "f8e1b8738bc552abe59a5b99e316b19b"
        page = (f'<a href="setlang.php?md5={md5}&lang=ru">ru</a>'
                f'<a href="get.php?md5={md5}&key=W8LRDYOR4ZPXS7OZ">GET</a>')
        with mock.patch.object(annas_backend, "_get",
                               return_value=_Response(text=page)):
            url = annas_backend.libgen_download_url(md5)

        self.assertEqual(
            url, f"https://libgen.li/get.php?md5={md5}&key=W8LRDYOR4ZPXS7OZ")


class DownloadTests(unittest.TestCase):
    def _item(self, **extra):
        item = {"title": "Moby Dick", "author": "Herman Melville",
                "source": book_backend.SOURCE_GUTENBERG,
                "download_url": "https://gutenberg/2701.epub",
                "format": book_backend.FORMAT_EPUB, "size_bytes": 4}
        item.update(extra)
        return item

    def test_a_finished_book_lands_in_the_books_folder(self):
        response = _Response(content=b"PK\x03\x04abcd",
                             headers={"Content-Length": "8"})
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(book_backend, "_open_stream",
                                   return_value=response):
                path = book_backend.download(
                    self._item(), folder,
                    progress_cb=lambda done, total: seen.append(done))

            self.assertEqual(
                os.path.relpath(path, folder),
                os.path.join(book_backend.BOOK_SUBFOLDER,
                             "Moby Dick - Herman Melville.epub"))
            self.assertTrue(os.path.isfile(path))
            self.assertFalse(os.path.exists(path + ".part"))
        self.assertTrue(seen)

    def test_an_error_page_is_rejected_instead_of_shelved(self):
        response = _Response(content=b"<!DOCTYPE html><html>nope</html>")
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(book_backend, "_open_stream",
                                   return_value=response):
                with self.assertRaises(RuntimeError):
                    book_backend.download(self._item(), folder)

            books = os.path.join(folder, book_backend.BOOK_SUBFOLDER)
            self.assertEqual(os.listdir(books), [])

    def test_cancelling_leaves_nothing_behind(self):
        cancel = threading.Event()
        cancel.set()
        response = _Response(content=b"PK\x03\x04abcd")
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(book_backend, "_open_stream",
                                   return_value=response):
                with self.assertRaises(book_backend.BookDownloadCancelled):
                    book_backend.download(self._item(), folder,
                                          cancel_event=cancel)

            books = os.path.join(folder, book_backend.BOOK_SUBFOLDER)
            self.assertEqual(os.listdir(books), [])

    def test_an_annas_archive_row_resolves_through_the_cascade(self):
        item = self._item(source=book_backend.SOURCE_ANNAS,
                          download_url="", md5="a" * 32, format="MOBI")
        response = _Response(content=b"BOOKMOBI-data")
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(annas_backend, "resolve_download",
                                    return_value="https://libgen/get") as resolve,
                  mock.patch.object(book_backend, "_open_stream",
                                    return_value=response)):
                path = book_backend.download(
                    item, folder, config={"annas_archive_key": "key"})

            self.assertTrue(path.endswith(".mobi"))
            self.assertTrue(os.path.isfile(path))
        self.assertEqual(resolve.call_args.kwargs["member_key"], "key")


class SearchTests(unittest.TestCase):
    def test_every_source_reports_through_on_site_and_asked(self):
        seen = []

        def fake(source):
            return lambda query, timeout=None: [
                book_backend._item(source, "one", f"{source} book", "A")]

        searchers = {source: fake(source)
                     for source in book_backend.ALL_SOURCES}
        with mock.patch.object(book_backend, "_SEARCHERS", searchers):
            items, answered, asked = book_backend.search(
                "moby dick", timeout_s=5,
                on_site=lambda source, rows: seen.append(source))

        self.assertEqual(sorted(asked), sorted(book_backend.ALL_SOURCES))
        self.assertEqual(sorted(answered), sorted(book_backend.ALL_SOURCES))
        self.assertEqual(sorted(seen), sorted(book_backend.ALL_SOURCES))
        self.assertEqual(len(items), len(book_backend.ALL_SOURCES))

    def test_a_failing_source_does_not_take_the_others_down(self):
        def broken(query, timeout=None):
            raise RuntimeError("site is down")

        def working(query, timeout=None):
            return [book_backend._item("Project Gutenberg", "1", "Book", "A")]

        with mock.patch.object(
                book_backend, "_SEARCHERS",
                {"Internet Archive": broken, "Project Gutenberg": working}):
            items, answered, _asked = book_backend.search(
                "moby dick", timeout_s=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(sorted(answered),
                         ["Internet Archive", "Project Gutenberg"])

    def test_a_superseded_search_stops_before_it_calls_back(self):
        stop = threading.Event()
        stop.set()
        called = []

        with mock.patch.object(
                book_backend, "_SEARCHERS",
                {"Internet Archive": lambda query, timeout=None: called.append(1)}):
            _items, answered, _asked = book_backend.search(
                "moby dick", timeout_s=5, stop=stop)

        self.assertEqual(called, [])
        self.assertEqual(answered, [])

    def test_only_the_chosen_sources_are_asked(self):
        with mock.patch.object(
                book_backend, "_SEARCHERS",
                {source: lambda query, timeout=None: []
                 for source in book_backend.ALL_SOURCES}):
            _items, _answered, asked = book_backend.search(
                "moby dick", timeout_s=5,
                sources=[book_backend.SOURCE_GUTENBERG])

        self.assertEqual(asked, [book_backend.SOURCE_GUTENBERG])


class SourceListTests(unittest.TestCase):
    def test_switched_off_libraries_are_left_out(self):
        enabled = book_backend.enabled_sources(
            [book_backend.SOURCE_ANNAS, book_backend.SOURCE_ARCHIVE])

        self.assertNotIn(book_backend.SOURCE_ANNAS, enabled)
        self.assertNotIn(book_backend.SOURCE_ARCHIVE, enabled)
        self.assertIn(book_backend.SOURCE_GUTENBERG, enabled)

    def test_file_names_survive_every_platform(self):
        self.assertEqual(
            book_backend.safe_filename('A: "Book"/Part <1>?'),
            "A Book Part 1")
        self.assertEqual(book_backend.safe_filename(""), "book")


if __name__ == "__main__":
    unittest.main()
