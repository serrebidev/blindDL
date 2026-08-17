# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Internet Archive media and free-audiobook backends."""

import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import requests

from blinddl import archive_backend, audiobook_backend, preview

from tests.test_book_backend import _Response


class ArchiveSearchTests(unittest.TestCase):
    DOCS = {"response": {"docs": [
        {"identifier": "dragnet", "title": "Dragnet", "creator": ["NBC"],
         "year": "1951", "item_size": "1048576"},
        {"identifier": "other", "title": "Something else", "creator": "X"},
    ]}}

    def test_a_category_query_excludes_sub_collections(self):
        with mock.patch.object(archive_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=self.DOCS)
            items = archive_backend.search_category(
                archive_backend.CATEGORY_OTR, "dragnet")
            query = http.return_value.get.call_args.kwargs["params"]["q"]

        self.assertIn("collection:(oldtimeradio)", query)
        self.assertIn("NOT mediatype:(collection)", query)
        self.assertEqual(items[0]["title"], "Dragnet")
        self.assertEqual(items[0]["creator"], "NBC")
        self.assertEqual(items[0]["file_size"], "1.0 MB")
        self.assertFalse(items[0]["video"])

    def test_video_categories_are_marked_as_video(self):
        with mock.patch.object(archive_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=self.DOCS)
            items = archive_backend.search_category(
                archive_backend.CATEGORY_MOVIES, "dragnet")

        self.assertTrue(items[0]["video"])
        self.assertTrue(archive_backend.is_video_category(
            archive_backend.CATEGORY_CLASSIC_TV))
        self.assertFalse(archive_backend.is_video_category(
            archive_backend.CATEGORY_OTR))

    def test_each_category_reports_separately(self):
        seen = []
        with mock.patch.object(archive_backend, "search_category",
                               side_effect=lambda source, query, **kwargs: [
                                   {"title": source, "creator": "",
                                    "source": source}]):
            items, answered, asked = archive_backend.search(
                "dragnet", timeout_s=5,
                on_site=lambda source, rows: seen.append(source))

        self.assertEqual(asked, archive_backend.ALL_SOURCES)
        self.assertEqual(sorted(seen), sorted(archive_backend.ALL_SOURCES))
        self.assertEqual(len(items), len(archive_backend.ALL_SOURCES))
        self.assertEqual(sorted(answered), sorted(archive_backend.ALL_SOURCES))

    def test_one_engine_asks_only_its_own_categories(self):
        with mock.patch.object(archive_backend, "search_category",
                               return_value=[]):
            _items, _answered, asked = archive_backend.search(
                "dragnet", timeout_s=5,
                sources=archive_backend.VIDEO_CATEGORIES)

        self.assertEqual(asked, archive_backend.VIDEO_CATEGORIES)

    def test_a_replaced_search_does_not_rank_what_it_cannot_deliver(self):
        # Ranking is the expensive half of a site's answer -- hundreds of
        # rows through difflib -- and a search the user has already replaced
        # has nowhere to put it. The site's reply is discarded either way.
        stop = threading.Event()
        rows = [{"title": f"Row {index}", "creator": "", "source": "x"}
                for index in range(5)]

        def answer(source, query, **kwargs):
            stop.set()
            return rows

        with mock.patch.object(archive_backend, "search_category",
                               side_effect=answer), \
                mock.patch.object(archive_backend, "_rank") as rank:
            archive_backend.search("dragnet", timeout_s=5, stop=stop)

        rank.assert_not_called()

    def test_switched_off_collections_are_left_out(self):
        enabled = archive_backend.enabled_sources(
            [archive_backend.CATEGORY_TV_NEWS],
            archive_backend.VIDEO_CATEGORIES)

        self.assertEqual(enabled, [archive_backend.CATEGORY_MOVIES,
                                   archive_backend.CATEGORY_CLASSIC_TV])


class ArchiveFileTests(unittest.TestCase):
    PAYLOAD = {"files": [
        {"name": "__ia_thumb.jpg", "format": "Item Tile"},
        {"name": "dragnet_02.mp3", "format": "VBR MP3", "size": "200",
         "length": "29:56", "title": "Big Actor"},
        {"name": "dragnet_01.mp3", "format": "VBR MP3", "size": "100",
         "length": "1796.64", "title": "Benny Trounsel"},
        {"name": "dragnet_01.ogg", "format": "Ogg Vorbis", "size": "90"},
    ]}

    def _files(self, video=False):
        with mock.patch.object(archive_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=self.PAYLOAD)
            return archive_backend.item_files("dragnet", video=video)

    def test_episodes_come_back_in_order_in_one_format(self):
        files = self._files()

        self.assertEqual([entry["file_name"] for entry in files],
                         ["dragnet_01.mp3", "dragnet_02.mp3"])
        self.assertEqual(files[0]["title"], "Benny Trounsel")
        self.assertAlmostEqual(files[0]["duration_s"], 1796.64)
        self.assertAlmostEqual(files[1]["duration_s"], 29 * 60 + 56)
        self.assertTrue(files[0]["direct_url"].endswith(
            "/download/dragnet/dragnet_01.mp3"))

    def test_an_item_without_the_wanted_media_says_so(self):
        with self.assertRaises(RuntimeError):
            self._files(video=True)

    def test_a_broken_item_blames_the_archive_not_the_item(self):
        # The Archive answers 200 with the fault in the body, which used to
        # read back as "no playable files" and sounded like a blindDL bug.
        with mock.patch.object(archive_backend, "_http") as http:
            http.return_value.get.return_value = _Response(
                payload={"error": "item metadata may be invalid"})
            with self.assertRaises(RuntimeError) as caught:
                archive_backend.item_files("broken", video=True)

        self.assertIn("cannot serve this item", str(caught.exception))
        self.assertIn("item metadata may be invalid", str(caught.exception))

    def test_an_unknown_identifier_says_there_is_no_record(self):
        with mock.patch.object(archive_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload={})
            with self.assertRaises(RuntimeError) as caught:
                archive_backend.item_files("gone", video=True)

        self.assertIn("no record", str(caught.exception))

    def test_a_slow_archive_reads_back_as_words_not_a_traceback(self):
        with mock.patch.object(archive_backend, "_http") as http:
            http.return_value.get.side_effect = \
                requests.exceptions.ReadTimeout("read timed out")
            with self.assertRaises(RuntimeError) as caught:
                archive_backend.item_files("slow", video=True)

        message = str(caught.exception)
        self.assertIn("try again", message)
        self.assertNotIn("HTTPSConnectionPool", message)

    def test_the_session_retries_a_slow_or_failing_archive(self):
        # A single attempt fails previews for items that answer on a retry;
        # the metadata endpoint routinely takes longer than a search does.
        adapter = archive_backend._http().get_adapter(
            "https://archive.org/metadata/x")
        retries = getattr(adapter, "max_retries")

        self.assertGreaterEqual(retries.total, 3)
        self.assertGreater(retries.backoff_factor, 0)
        self.assertIn(503, retries.status_forcelist)
        self.assertGreaterEqual(archive_backend.METADATA_TIMEOUT_S[1], 45)

    def test_preview_plays_the_first_file_of_an_item(self):
        item = {"kind": "archive", "title": "Dragnet", "identifier": "dragnet"}
        with mock.patch.object(archive_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=self.PAYLOAD)
            location, title = preview.resolve_search_result(
                item, audio_only=True, config={})

        self.assertTrue(location.endswith("dragnet_01.mp3"))
        self.assertEqual(title, "Dragnet")

    def test_preview_of_a_chosen_episode_needs_no_lookup(self):
        item = {"kind": "archive", "title": "Episode 1",
                "direct_url": "https://archive.org/download/d/ep1.mp3"}
        with mock.patch.object(archive_backend, "_http") as http:
            location, _title = preview.resolve_search_result(
                item, audio_only=True, config={})
            http.assert_not_called()

        self.assertEqual(location, "https://archive.org/download/d/ep1.mp3")


class ArchiveDownloadTests(unittest.TestCase):
    def test_a_single_file_lands_next_to_the_other_downloads(self):
        entry = {"title": "Episode 1", "file_name": "ep1.mp3",
                 "identifier": "dragnet", "collection_title": "Dragnet",
                 "direct_url": "https://x/ep1.mp3", "size_bytes": 4}
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(archive_backend, "_http") as http:
                http.return_value.get.return_value = _Response(content=b"data")
                path = archive_backend.download(entry, folder)

            self.assertEqual(os.path.relpath(path, folder), "ep1.mp3")
            self.assertTrue(os.path.isfile(path))

    def test_a_whole_item_becomes_a_numbered_folder(self):
        item = {"title": "Dragnet", "identifier": "dragnet", "video": False}
        files = [
            {"title": "One", "file_name": "a.mp3", "direct_url": "https://x/a",
             "size_bytes": 4},
            {"title": "Two", "file_name": "b.mp3", "direct_url": "https://x/b",
             "size_bytes": 4},
        ]
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(archive_backend, "item_files",
                                    return_value=files),
                  mock.patch.object(archive_backend, "_http") as http):
                http.return_value.get.return_value = _Response(content=b"data")
                path = archive_backend.download(item, folder)

            self.assertEqual(sorted(os.listdir(path)),
                             ["01 - a.mp3", "02 - b.mp3"])

    def test_cancelling_removes_the_partial_file(self):
        cancel = threading.Event()
        cancel.set()
        entry = {"title": "Episode 1", "file_name": "ep1.mp3",
                 "direct_url": "https://x/ep1.mp3", "identifier": "d"}
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(archive_backend, "_http") as http:
                http.return_value.get.return_value = _Response(content=b"data")
                with self.assertRaises(
                        archive_backend.ArchiveDownloadCancelled):
                    archive_backend.download(entry, folder,
                                             cancel_event=cancel)

            self.assertEqual(os.listdir(folder), [])

    def test_a_dropped_transfer_resumes_instead_of_starting_again(self):
        # The Archive closes long transfers part-way through. Chapter seven
        # of a thirteen-chapter book used to end the whole download there.
        class _Dropped(_Response):
            def iter_content(self, chunk_size=1):
                yield b"first half "
                raise requests.exceptions.ChunkedEncodingError("connection lost")

        responses = [_Dropped(), _Response(content=b"second half",
                                           status_code=206)]
        entry = {"title": "Seven", "file_name": "07.mp3", "identifier": "d",
                 "direct_url": "https://x/07.mp3", "size_bytes": 22}
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(archive_backend, "_http") as http,
                  mock.patch.object(archive_backend.time, "sleep")):
                http.return_value.get.side_effect = (
                    lambda *args, **kwargs: responses.pop(0))
                path = archive_backend.download(entry, folder)

            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"first half second half")
            # The second attempt asked only for what was missing.
            self.assertEqual(
                http.return_value.get.call_args.kwargs["headers"],
                {"Range": "bytes=11-"})

    def test_a_server_that_ignores_the_range_is_not_appended_to_twice(self):
        class _Dropped(_Response):
            def iter_content(self, chunk_size=1):
                yield b"partial"
                raise requests.exceptions.ChunkedEncodingError("connection lost")

        # Answering 200 to a Range request means the whole file again, so
        # what is already on disk is worthless rather than a head start.
        responses = [_Dropped(), _Response(content=b"whole file",
                                           status_code=200)]
        entry = {"title": "Seven", "file_name": "07.mp3", "identifier": "d",
                 "direct_url": "https://x/07.mp3", "size_bytes": 10}
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(archive_backend, "_http") as http,
                  mock.patch.object(archive_backend.time, "sleep")):
                http.return_value.get.side_effect = (
                    lambda *args, **kwargs: responses.pop(0))
                path = archive_backend.download(entry, folder)

            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"whole file")

    def test_a_file_the_archive_keeps_dropping_says_what_to_do(self):
        class _Dropped(_Response):
            def iter_content(self, chunk_size=1):
                yield b"some"
                raise requests.exceptions.ChunkedEncodingError("connection lost")

        entry = {"title": "Seven", "file_name": "07.mp3", "identifier": "d",
                 "direct_url": "https://x/07.mp3", "size_bytes": 99}
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(archive_backend, "_http") as http,
                  mock.patch.object(archive_backend.time, "sleep")):
                http.return_value.get.side_effect = (
                    lambda *args, **kwargs: _Dropped())
                with self.assertRaises(RuntimeError) as caught:
                    archive_backend.download(entry, folder)

            self.assertEqual(
                http.return_value.get.call_count,
                archive_backend.DOWNLOAD_ATTEMPTS)
            self.assertIn("picks up where it stopped", str(caught.exception))
            # The part file is what the next run resumes from.
            self.assertEqual(os.listdir(folder), ["07.mp3.part"])

    def test_a_file_the_archive_does_not_have_is_not_asked_for_five_times(self):
        error = requests.exceptions.HTTPError("404")
        error.response = SimpleNamespace(status_code=404)

        class _Missing(_Response):
            def raise_for_status(self):
                raise error

        entry = {"title": "Seven", "file_name": "07.mp3", "identifier": "d",
                 "direct_url": "https://x/07.mp3", "size_bytes": 1}
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(archive_backend, "_http") as http,
                  mock.patch.object(archive_backend.time, "sleep")):
                http.return_value.get.side_effect = (
                    lambda *args, **kwargs: _Missing())
                with self.assertRaises(RuntimeError) as caught:
                    archive_backend.download(entry, folder)

        self.assertEqual(http.return_value.get.call_count, 1)
        self.assertIn("will not serve", str(caught.exception))


class AudiobookTests(unittest.TestCase):
    def test_an_audiobooker_book_becomes_a_result_row(self):
        book = SimpleNamespace(
            title="The Return of Sherlock Holmes",
            authors=[SimpleNamespace(first_name="Arthur Conan",
                                     last_name="Doyle")],
            narrators=[SimpleNamespace(first_name="Mark", last_name="Smith")],
            streams=["https://archive.org/download/x/01.mp3",
                     "https://archive.org/download/x/02.mp3"],
            chapters=[object(), object()],
            runtime=40018, year=1905, source="Librivox",
            external_ids={"librivox_id": "123"},
        )

        item = audiobook_backend._from_audiobook(book)

        self.assertEqual(item["kind"], "audiobook")
        self.assertEqual(item["source"], "LibriVox")
        self.assertEqual(item["author"], "Arthur Conan Doyle")
        self.assertEqual(item["narrator"], "Mark Smith")
        self.assertEqual(item["chapters"], 2)
        self.assertEqual(item["duration_s"], 40018)
        self.assertEqual(item["identifier"], "123")

    def test_source_labels_read_as_names_not_class_names(self):
        self.assertEqual(audiobook_backend.source_label("Librivox"), "LibriVox")
        self.assertEqual(
            audiobook_backend.source_label("StephenKingAudioBooks"),
            "Stephen King Audio Books")

    def test_the_archive_source_works_without_audiobooker(self):
        # A missing audiobooker must not take the whole engine down, and the
        # sites it would have provided are not offered rather than failing.
        with mock.patch.object(audiobook_backend, "_audiobooker_classes",
                               return_value={}):
            self.assertEqual(audiobook_backend.all_sources(),
                             [audiobook_backend.SOURCE_ARCHIVE_AUDIO])
            self.assertEqual(
                audiobook_backend.search_audiobooker("Librivox", "sherlock"),
                [])

    def test_installed_audiobooker_sites_join_the_source_list(self):
        with mock.patch.object(audiobook_backend, "_audiobooker_classes",
                               return_value={"Librivox": object(),
                                             "LoyalBooks": object()}):
            sources = audiobook_backend.all_sources()

        self.assertEqual(sources, [audiobook_backend.SOURCE_ARCHIVE_AUDIO,
                                   "Librivox", "LoyalBooks"])

    def test_chapters_download_into_one_numbered_folder(self):
        item = {"title": "Sherlock", "author": "Doyle",
                "backend_source": audiobook_backend.SOURCE_ARCHIVE_AUDIO,
                "identifier": "sherlock", "size_bytes": 8}
        chapters = [("https://x/01.mp3", "01.mp3"),
                    ("https://x/02.mp3", "02.mp3")]
        with tempfile.TemporaryDirectory() as folder:
            with (mock.patch.object(audiobook_backend, "archive_streams",
                                    return_value=chapters),
                  mock.patch.object(audiobook_backend, "_http") as http):
                http.return_value.get.return_value = _Response(content=b"data")
                path = audiobook_backend.download(item, folder)

            self.assertEqual(
                os.path.relpath(path, folder),
                os.path.join(audiobook_backend.AUDIOBOOK_SUBFOLDER,
                             "Sherlock - Doyle"))
            self.assertEqual(sorted(os.listdir(path)),
                             ["01 - 01.mp3", "02 - 02.mp3"])

    def test_a_chapter_already_on_disk_is_not_fetched_again(self):
        item = {"title": "Sherlock", "author": "",
                "backend_source": audiobook_backend.SOURCE_ARCHIVE_AUDIO,
                "identifier": "sherlock"}
        chapters = [("https://x/01.mp3", "01.mp3")]
        with tempfile.TemporaryDirectory() as folder:
            book_folder = os.path.join(
                folder, audiobook_backend.AUDIOBOOK_SUBFOLDER, "Sherlock")
            os.makedirs(book_folder)
            with open(os.path.join(book_folder, "01 - 01.mp3"), "wb") as handle:
                handle.write(b"already here")

            with (mock.patch.object(audiobook_backend, "archive_streams",
                                    return_value=chapters),
                  mock.patch.object(audiobook_backend, "_http") as http):
                audiobook_backend.download(item, folder)
                http.return_value.get.assert_not_called()

    def test_librivox_chapters_prefer_mp3_and_keep_their_order(self):
        payload = {"files": [
            {"name": "book_02.mp3"},
            {"name": "book_01.mp3"},
            {"name": "book_01.ogg"},
            {"name": "cover.jpg"},
        ]}
        with mock.patch.object(audiobook_backend, "_http") as http:
            http.return_value.get.return_value = _Response(payload=payload)
            streams = audiobook_backend.archive_streams("book")

        self.assertEqual([name for _url, name in streams],
                         ["book_01.mp3", "book_02.mp3"])

    def test_preview_plays_an_audiobook_chapter(self):
        item = {"kind": "audiobook", "title": "Sherlock",
                "streams": ["https://x/01.mp3"]}

        location, title = preview.resolve_search_result(
            item, audio_only=True, config={})

        self.assertEqual(location, "https://x/01.mp3")
        self.assertEqual(title, "Sherlock")


if __name__ == "__main__":
    unittest.main()
