# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Tests for how well a music result is judged to answer a search.

The cases here are real: they are what the music sites actually returned
for "Naomi streamer", a search for Naomi Striemer typed the way a person
types an unfamiliar name.
"""

import unittest

from blinddl.music_match import (
    MIN_MATCH_SCORE,
    rank_music,
    score_music,
)


class ScoreTests(unittest.TestCase):
    def test_a_near_miss_in_the_artist_still_finds_the_artist(self):
        """"streamer" and "striemer" are one transposition apart."""
        for title in ("Run", "You Are Beautiful",
                      "From Heaven With Love (Bonus Track)"):
            with self.subTest(title=title):
                score = score_music("Naomi streamer", title, "Naomi Striemer")
                self.assertGreaterEqual(score, MIN_MATCH_SCORE)

    def test_a_long_title_by_the_right_artist_is_not_punished(self):
        """The fault that made the book scorer the wrong tool for music."""
        short = score_music("Naomi streamer", "Run", "Naomi Striemer")
        long = score_music("Naomi streamer",
                           "From Heaven With Love (Bonus Track)",
                           "Naomi Striemer")
        self.assertLess(abs(short - long), 5.0)

    def test_a_word_that_merely_looks_similar_is_not_the_word(self):
        """"dreamers" is not "streamer", however much it resembles it."""
        self.assertEqual(
            score_music("Naomi streamer", "Dreamers", "Claire Denamur"), 0.0)

    def test_a_result_sharing_no_word_scores_nothing(self):
        self.assertEqual(
            score_music("Naomi streamer", "道成肉身",
                        "抚顺望花"), 0.0)

    def test_half_a_query_answered_is_not_enough(self):
        """Sharing only the forename is what buried the real answers."""
        score = score_music("Naomi streamer",
                            "Naomi ke Chanda Kate lagal (Bhojpuri)",
                            "Sunny Raj, Amrita Raj")
        self.assertLess(score, MIN_MATCH_SCORE)

    def test_a_song_title_is_matched_as_readily_as_an_artist(self):
        self.assertGreaterEqual(
            score_music("Speechless", "Speechless (Naomi Scott cover)",
                        "Some Uploader"),
            MIN_MATCH_SCORE)

    def test_the_album_counts_too(self):
        score = score_music("random access memories", "Get Lucky",
                            "Daft Punk", "Random Access Memories")
        self.assertGreaterEqual(score, MIN_MATCH_SCORE)

    def test_a_short_word_has_to_match_exactly(self):
        """Fuzzy matching on three letters makes everything match."""
        self.assertEqual(score_music("abc", "abd", "xyz"), 0.0)

    def test_an_empty_query_or_result_scores_nothing(self):
        self.assertEqual(score_music("", "Run", "Naomi Striemer"), 0.0)
        self.assertEqual(score_music("Naomi", "", ""), 0.0)

    def test_a_terser_result_edges_out_a_padded_one(self):
        plain = score_music("get lucky", "Get Lucky", "Daft Punk")
        padded = score_music(
            "get lucky", "Get Lucky (Extended Club Mix Bonus Version)",
            "Daft Punk Tribute Band Collective")
        self.assertGreater(plain, padded)


class RankTests(unittest.TestCase):
    def _rows(self):
        return [
            {"title": "道成肉身", "artist": "抚顺"},
            {"title": "Dreamers", "artist": "Claire Denamur"},
            {"title": "You Are Beautiful", "artist": "Naomi Striemer"},
            {"title": "Run", "artist": "Naomi Striemer"},
        ]

    def test_the_answers_come_first_and_the_noise_is_dropped(self):
        ranked = rank_music(self._rows(), "Naomi streamer")
        self.assertEqual([row["title"] for row in ranked],
                         ["Run", "You Are Beautiful"])

    def test_a_site_that_matched_nothing_contributes_nothing(self):
        """One site's rows, with three dozen other sites still answering."""
        noise = [{"title": "Dreamers", "artist": "Claire Denamur"}]
        self.assertEqual(rank_music(noise, "Naomi streamer",
                                    allow_empty=True), [])

    def test_a_search_nothing_could_answer_still_shows_its_best_guesses(self):
        noise = [{"title": "Dreamers", "artist": "Claire Denamur"},
                 {"title": "Naomi ke Chanda", "artist": "Sunny Raj"}]
        ranked = rank_music(noise, "Naomi streamer")
        self.assertEqual(len(ranked), 2)
        # Even then they arrive in the order of how close they came.
        self.assertEqual(ranked[0]["title"], "Naomi ke Chanda")

    def test_equal_scores_keep_the_order_they_arrived_in(self):
        rows = [{"title": "Run", "artist": "Naomi Striemer"},
                {"title": "Run", "artist": "Naomi Striemer"}]
        rows[0]["id"], rows[1]["id"] = "first", "second"
        ranked = rank_music(rows, "Naomi streamer")
        self.assertEqual([row["id"] for row in ranked], ["first", "second"])

    def test_every_row_carries_its_score_onward(self):
        """The results list sorts the whole search by it later."""
        ranked = rank_music(self._rows(), "Naomi streamer")
        for row in ranked:
            self.assertIsInstance(row["score"], float)


if __name__ == "__main__":
    unittest.main()
