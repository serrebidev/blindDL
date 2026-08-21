# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""The search-type vocabulary the Search tab and the music backends share."""

import unittest

from blinddl import search_kind


class SearchKindTests(unittest.TestCase):
    def test_unknown_and_missing_types_mean_best_match(self):
        # Saved config and callers that predate the setting both reach the
        # backends, so anything unrecognised has to mean "as before".
        self.assertEqual(search_kind.normalize(None), search_kind.KIND_BEST)
        self.assertEqual(search_kind.normalize(""), search_kind.KIND_BEST)
        self.assertEqual(search_kind.normalize("nonsense"), search_kind.KIND_BEST)
        self.assertEqual(
            search_kind.normalize(search_kind.KIND_ALBUM), search_kind.KIND_ALBUM
        )
        self.assertEqual(search_kind.label("nonsense"), "Best match")

    def test_the_five_types_read_the_way_the_choice_lists_them(self):
        self.assertEqual(
            search_kind.KIND_LABEL_LIST,
            ["Best match", "Track title", "Album", "Playlist", "Artist"],
        )
        self.assertTrue(search_kind.is_album(search_kind.KIND_ALBUM))
        self.assertFalse(search_kind.is_album(search_kind.KIND_ARTIST))

    def test_a_title_match_ignores_punctuation_and_extra_words(self):
        self.assertTrue(
            search_kind.matches("Harder, Better, Faster, Stronger",
                                "harder better faster stronger")
        )
        self.assertTrue(
            search_kind.matches("One More Time (Radio Edit)", "one more time")
        )
        # Every word has to be there, so a near-miss is left out rather than
        # scored and kept.
        self.assertFalse(search_kind.matches("Baby One More", "one more time"))
        self.assertFalse(search_kind.matches("", "one more time"))
        # An empty query narrows nothing, which is what best match is.
        self.assertTrue(search_kind.matches("Anything", ""))

    def test_an_albums_track_count_is_read_out_with_it(self):
        self.assertEqual(search_kind.album_type_label(14), "Album, 14 tracks")
        self.assertEqual(search_kind.album_type_label(1), "Album, 1 track")
        # A site that did not say how many tracks are on it says nothing,
        # rather than "0 tracks", which would read as an empty album.
        self.assertEqual(search_kind.album_type_label(0), "Album")
        self.assertEqual(search_kind.album_type_label(None), "Album")
        self.assertEqual(search_kind.album_type_label("many"), "Album")

    def test_a_release_is_called_what_the_catalogue_calls_it(self):
        # An artist's page is mostly not albums. Reading "Single" or "EP" is
        # the difference between a discography a user can navigate and three
        # hundred rows that all say the same word.
        self.assertEqual(
            search_kind.album_type_label(1, "single"), "Single, 1 track")
        self.assertEqual(search_kind.album_type_label(6, "ep"), "EP, 6 tracks")
        self.assertEqual(
            search_kind.album_type_label(40, "compilation"),
            "Compilation, 40 tracks",
        )
        # A site that publishes no record type, or one nobody has heard of,
        # still has to read as something: an album.
        self.assertEqual(search_kind.album_type_label(9), "Album, 9 tracks")
        self.assertEqual(
            search_kind.album_type_label(9, "bootleg"), "Album, 9 tracks")
        self.assertEqual(search_kind.release_noun(None), "Album")
        self.assertEqual(search_kind.release_noun("SINGLE"), "Single")


if __name__ == "__main__":
    unittest.main()
