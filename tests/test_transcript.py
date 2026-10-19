# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest

from blinddl import transcript

# The shape YouTube's automatic captions arrive in: the whole box is
# redrawn on every cue, so each line appears twice as it scrolls up.
ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
Hello there

00:00:03.000 --> 00:00:05.000
Hello there
and welcome to the show.

00:00:09.500 --> 00:00:11.000
<00:00:09.500><c>back</c> again

NOTE a comment the site added

00:00:11.000 --> 00:00:13.000 align:start position:10%
back again
"""

SUBRIP = """1
00:00:01,000 --> 00:00:03,000
One line

2
00:00:03,000 --> 00:00:05,000
Another line
"""


class TranscriptTextTests(unittest.TestCase):
    def test_rolling_captions_keep_each_line_once(self):
        self.assertEqual(
            transcript.transcript(ROLLING_VTT),
            "Hello there and welcome to the show.\n\nback again\n",
        )

    def test_a_pause_starts_a_paragraph_and_a_breath_does_not(self):
        text = transcript.transcript(ROLLING_VTT)
        # One paragraph for the two cues that run into each other, a second
        # for the line after the four and a half second gap.
        self.assertEqual(len(text.strip().split("\n\n")), 2)

    def test_a_pause_inside_a_sentence_does_not_start_a_paragraph(self):
        # Speakers pause mid-sentence, and a transcript that breaks there
        # reads as a list of fragments rather than as what was said.
        text = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\nand then we went\n\n"
            "00:00:08.000 --> 00:00:09.000\nto the park.\n")
        self.assertEqual(
            transcript.transcript(text), "and then we went to the park.\n")

    def test_subrip_parses_with_its_numbers_and_commas(self):
        self.assertEqual(
            transcript.transcript(SUBRIP), "One line Another line\n")

    def test_speaking_a_line_again_later_keeps_it(self):
        text = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\nCome back\n\n"
            "00:00:02.000 --> 00:00:03.000\nto me.\n\n"
            "00:00:20.000 --> 00:00:21.000\nCome back\n")
        self.assertEqual(
            transcript.transcript(text), "Come back to me.\n\nCome back\n")

    def test_a_file_with_no_cues_has_no_transcript(self):
        self.assertEqual(transcript.transcript("WEBVTT\n\nNOTE nothing here\n"), "")


class TranscriptFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def _write(self, name, text):
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_transcript_is_written_beside_the_media_and_the_captions_go(self):
        media = self._write("Show [dQw4w9].mp3", "")
        captions = self._write("Show [dQw4w9].en.vtt", SUBRIP)
        target = transcript.write_transcript(media)
        self.assertEqual(target, os.path.join(self.folder, "Show [dQw4w9].txt"))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "One line Another line\n")
        self.assertFalse(os.path.exists(captions))
        self.assertTrue(os.path.exists(media))

    def test_another_language_is_left_alone(self):
        media = self._write("Show [dQw4w9].mp3", "")
        other = self._write("Show [dQw4w9].de.vtt", SUBRIP)
        self.assertEqual(transcript.write_transcript(media), "")
        self.assertTrue(os.path.exists(other))
        self.assertFalse(os.path.exists(
            os.path.join(self.folder, "Show [dQw4w9].txt")))

    def test_all_caption_languages_go_after_a_transcript_is_saved(self):
        media = self._write("Show [dQw4w9].mp3", "")
        english = self._write("Show [dQw4w9].en.vtt", SUBRIP)
        french = self._write("Show [dQw4w9].fr.vtt", SUBRIP)
        other = self._write("Show [dQw4w9].fr.json3", "{}")
        unrelated = self._write("Other.fr.vtt", SUBRIP)

        target = transcript.write_transcript(media)

        self.assertTrue(os.path.exists(target))
        self.assertFalse(os.path.exists(english))
        self.assertFalse(os.path.exists(french))
        self.assertFalse(os.path.exists(other))
        self.assertTrue(os.path.exists(unrelated))

    def test_captions_with_nothing_readable_are_kept(self):
        media = self._write("Show [dQw4w9].mp3", "")
        captions = self._write("Show [dQw4w9].en.vtt", "WEBVTT\n\n")
        self.assertEqual(transcript.write_transcript(media), "")
        self.assertTrue(os.path.exists(captions))

    def test_a_second_caption_shape_goes_with_the_rest(self):
        # One request writes one caption file per language, and the second
        # language may be a shape this reader cannot take. Both go once the
        # words are in the transcript.
        media = self._write("Show [dQw4w9].mp3", "")
        self._write("Show [dQw4w9].en.vtt", SUBRIP)
        other = self._write("Show [dQw4w9].en-orig.json3", "{}")

        target = transcript.write_transcript(media)

        self.assertTrue(os.path.exists(target))
        self.assertFalse(os.path.exists(other))

    def test_a_caption_in_no_readable_shape_is_left_alone(self):
        # shortcut: ttml, json3 and the player-owned shapes are not read
        # here, so a site with none of vtt or srt keeps what it sent rather
        # than losing it to a transcript that came out empty.
        media = self._write("Show [dQw4w9].mp3", "")
        other = self._write("Show [dQw4w9].en.json3", "{}")

        self.assertEqual(transcript.write_transcript(media), "")
        self.assertTrue(os.path.exists(other))

    def test_media_without_captions_writes_nothing(self):
        media = self._write("Song [abc123].mp3", "")
        self.assertEqual(transcript.write_transcript(media), "")
        self.assertEqual(os.listdir(self.folder), ["Song [abc123].mp3"])


if __name__ == "__main__":
    unittest.main()
