# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest

from blinddl import describe

DESCRIBED_VERSIONS = [
    {"title": "How Sound Works (Audio Described)", "url": "u1",
     "uploader": "Studio", "duration": 600},
    {"title": "How Sound Works - trailer", "url": "u2"},
    {"title": "Audio description of something else entirely", "url": "u3"},
    {"title": "How Sound Works [AD] by the studio", "url": "u4"},
]


class CandidateTests(unittest.TestCase):
    def test_only_a_published_described_version_is_offered(self):
        # A search for a title turns up plenty that is not its described
        # version. Both halves are needed: it has to say it is described,
        # and it has to be about the same thing.
        found = describe.candidate_versions("How Sound Works",
                                            DESCRIBED_VERSIONS)
        self.assertEqual([item["url"] for item in found], ["u1", "u4"])

    def test_a_described_version_of_another_video_is_not_offered(self):
        self.assertEqual(
            describe.candidate_versions("How Sound Works", [
                {"title": "Something else entirely, audio described",
                 "url": "u"}]), [])

    def test_two_unrelated_videos_sharing_only_their_packaging_do_not_match(self):
        # "Official trailer, audio described" describes both every trailer
        # there has ever been and none of them in particular.
        self.assertEqual(
            describe.candidate_versions("Frozen trailer", [
                {"title": "Avatar: The Way of Water | Audio Described "
                          "Official Trailer", "url": "u"}]), [])
        self.assertEqual(
            describe.candidate_versions("Frozen trailer", [
                {"title": "Frozen - Trailer with Audio Description",
                 "url": "u"}])[0]["url"], "u")

    def test_the_candidate_sharing_most_of_the_title_comes_first(self):
        found = describe.candidate_versions("How Sound Works Explained", [
            {"title": "How Sound Works, audio described", "url": "half"},
            {"title": "How Sound Works Explained, audio described",
             "url": "all"},
        ])
        self.assertEqual([item["url"] for item in found], ["all", "half"])


class DescribedTrackTests(unittest.TestCase):
    def test_a_label_is_what_marks_a_described_track(self):
        # Nothing about the stream itself says it is a description; the
        # site says so in the track's own name, and that is what is read.
        tracks = describe.described_tracks([
            {"format_id": "251", "vcodec": "none", "acodec": "opus",
             "format_note": "medium"},
            {"format_id": "251-ad", "vcodec": "none", "acodec": "opus",
             "format_note": "English descriptive"},
            {"format_id": "137", "vcodec": "avc1", "acodec": "none",
             "format_note": "1080p descriptive"},
        ])
        self.assertEqual([track["format_id"] for track in tracks], ["251-ad"])

    def test_a_second_audio_track_named_elsewhere_still_counts(self):
        tracks = describe.described_tracks([
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a",
             "audio_track": {"display_name": "English descriptive"}},
        ])
        self.assertEqual(len(tracks), 1)


class AnswerTests(unittest.TestCase):
    def test_moments_after_one_hundred_minutes_are_kept(self):
        self.assertEqual(
            describe.parse_description("00:03 | Start.\n120:05 | Later.\n"),
            [(3, "Start."), (7205, "Later.")],
        )

    def test_an_answer_becomes_timed_moments(self):
        answer = (
            "00:03 | A woman in a red coat walks into the room.\n"
            "00:41 - He points at the chart.\n"
            "That last line is a summary, not a moment.\n")
        self.assertEqual(describe.parse_description(answer), [
            (3, "A woman in a red coat walks into the room."),
            (41, "He points at the chart."),
        ])

    def test_a_batch_asks_for_moments_by_the_clock(self):
        prompt = describe.description_prompt([(0.0, "a.jpg"), (72.5, "b.jpg")])
        self.assertIn("00:00 - a.jpg", prompt)
        self.assertIn("01:12 - b.jpg", prompt)
        self.assertIn("Read the images in order", prompt)

    def test_the_pause_lasts_as_long_as_the_words_take(self):
        self.assertEqual(describe.reading_seconds(""),
                         describe.MINIMUM_READING_S)
        self.assertEqual(describe.reading_seconds("one two"), 2.0)
        self.assertAlmostEqual(
            describe.reading_seconds(" ".join(["word"] * 25)), 10.0)


class ShotTests(unittest.TestCase):
    def test_cuts_come_from_the_scene_score_and_start_at_the_beginning(self):
        seen = []

        def runner(command):
            seen.append(command)
            return ("pts_time:0.000\npts_time:4.500\npts_time:4.520\n"
                    "pts_time:9.000\n")

        self.assertEqual(describe.shot_times("film.mp4", run=runner),
                         [0.0, 4.5, 9.0])
        self.assertEqual(seen[0][0], "ffmpeg")
        self.assertIn("film.mp4", seen[0])


class DescriptionFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def _media(self):
        path = os.path.join(self.folder, "Show [dQw4w9].mp4")
        open(path, "wb").close()
        return path

    def test_a_machine_made_description_says_so_on_every_reading(self):
        text_path = describe.write_description(
            self._media(), [(3, "A dog barks."), (72, "The room empties.")])
        with open(text_path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("written by a machine", text)
        self.assertIn("00:03  A dog barks.", text)
        self.assertIn("01:12  The room empties.", text)

    def test_the_timed_file_is_what_the_player_reads_out(self):
        media = self._media()
        describe.write_description(media, [(12, "Second."), (3, "First.")])
        self.assertEqual(describe.load_description(media),
                         [(3.0, "First."), (12.0, "Second.")])

    def test_media_with_no_description_reads_as_nothing(self):
        self.assertEqual(describe.load_description(self._media()), [])

    def test_a_description_written_by_a_person_carries_no_warning(self):
        text_path = describe.write_description(
            self._media(), [(3, "A dog barks.")], source="published")
        with open(text_path, encoding="utf-8") as handle:
            self.assertNotIn("written by a machine", handle.read())


class MakeDescriptionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.media = os.path.join(self.folder, "Talk [abc].mp4")
        open(self.media, "wb").close()

    def test_every_shot_is_read_and_the_answers_become_the_file(self):
        asked = []
        said = []

        def runner(command):
            if "showinfo" in " ".join(command):
                return "pts_time:0.000\npts_time:10.000\n"
            return ""

        def asker(prompt):
            asked.append(prompt)
            return ("00:00 | A kitchen, and someone at the table.\n"
                    "00:10 | The same kitchen, now empty.\n")

        written = describe.make_description(
            self.media, log=said.append, run=runner, ask=asker)

        self.assertTrue(os.path.isfile(written))
        self.assertEqual(
            [text for _at, text in describe.load_description(self.media)],
            ["A kitchen, and someone at the table.",
             "The same kitchen, now empty."])
        self.assertEqual(len(asked), 1)
        self.assertIn("Describing shots 1 to 2", " ".join(said))

    def test_nothing_readable_back_is_an_error_rather_than_an_empty_file(self):
        def runner(command):
            return "pts_time:0.000\n" if "showinfo" in " ".join(command) else ""

        with self.assertRaises(RuntimeError):
            describe.make_description(
                self.media, run=runner, ask=lambda prompt: "I could not help.")
        self.assertEqual(describe.load_description(self.media), [])


if __name__ == "__main__":
    unittest.main()
