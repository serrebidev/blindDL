# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Tests for the tags written onto finished music downloads.

Real FLAC and MP3 files are built here rather than mocked -- both formats
can be constructed from a header alone, so the tag writers are exercised
through mutagen exactly as they run in production. The two lookup services
are mocked; nothing in this file touches the network.
"""

import os
import struct
import tempfile
import unittest
from unittest import mock

from mutagen.flac import FLAC
from mutagen.id3 import ID3

from blinddl import music_tags


def _flac_path(directory):
    """A valid, empty FLAC: header and STREAMINFO, no audio frames."""
    stream_info = struct.pack(">HH", 4096, 4096)
    stream_info += b"\x00\x00\x00" * 2
    # 20 bits sample rate, 3 bits channels - 1, 5 bits depth - 1, 36 bits samples
    stream_info += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big")
    stream_info += b"\x00" * 16
    path = os.path.join(directory, "track.flac")
    with open(path, "wb") as handle:
        handle.write(b"fLaC" + bytes([0x80])
                     + len(stream_info).to_bytes(3, "big") + stream_info)
    return path


def _mp3_path(directory):
    """A valid MP3: eight silent MPEG-1 Layer III frames."""
    path = os.path.join(directory, "track.mp3")
    with open(path, "wb") as handle:
        handle.write((b"\xff\xfb\x90\x00" + b"\x00" * 413) * 8)
    return path


class _SongInfo:
    """The parts of a musicdl SongInfo the tagger reads."""

    def __init__(self, **fields):
        self.song_name = fields.pop("song_name", "NULL")
        self.singers = fields.pop("singers", "NULL")
        self.album = fields.pop("album", "NULL")
        self.cover_url = fields.pop("cover_url", "NULL")
        self.raw_data = fields.pop("raw_data", {})
        self.source = fields.pop("source", "FreeQobuzMusicClient")
        self.work_dir = ""
        self._save_path = None


QOBUZ_ITEM = {
    "id": 12345,
    "title": "A Title",
    "isrc": "GBAYE0601498",
    "track_number": 3,
    "media_number": 1,
    "copyright": "(C) 2013 A Label",
    "composer": {"name": "A Composer"},
    "performer": {"name": "A Performer"},
    "album": {
        "title": "An Album",
        "artist": {"name": "An Album Artist"},
        "label": {"name": "A Label"},
        "genre": {"name": "Electronic"},
        "tracks_count": 13,
        "media_count": 1,
        "release_date_original": "2013-05-17",
        "released_at": "2013-05-20",
        "image": {"large": "https://example.invalid/large.jpg",
                  "small": "https://example.invalid/small.jpg"},
    },
}


class ReadingWhatTheSiteSentTests(unittest.TestCase):
    def test_a_qobuz_result_gives_up_its_whole_release(self):
        """The catalogue entry blindDL already holds is the first source."""
        song = _SongInfo(song_name="A Title", singers="A Performer",
                         album="An Album",
                         raw_data={"search": QOBUZ_ITEM})
        tags, cover = music_tags.tags_from_song_info(song)
        self.assertEqual(tags["title"], "A Title")
        self.assertEqual(tags["artist"], "A Performer")
        self.assertEqual(tags["album"], "An Album")
        self.assertEqual(tags["albumartist"], "An Album Artist")
        self.assertEqual(tags["composer"], "A Composer")
        self.assertEqual(tags["label"], "A Label")
        self.assertEqual(tags["genre"], "Electronic")
        self.assertEqual(tags["isrc"], "GBAYE0601498")
        self.assertEqual(tags["tracknumber"], "3")
        self.assertEqual(tags["tracktotal"], "13")
        self.assertEqual(tags["discnumber"], "1")
        self.assertEqual(tags["copyright"], "(C) 2013 A Label")
        # The original release date, not the later streaming date.
        self.assertEqual(tags["date"], "2013-05-17")
        self.assertEqual(cover, "https://example.invalid/large.jpg")

    def test_musicdls_spelling_of_nothing_is_not_a_tag(self):
        """musicdl writes "NULL" where a field is absent; that is not a value."""
        song = _SongInfo(song_name="A Title", singers="NULL", album="null",
                         cover_url="NULL")
        tags, cover = music_tags.tags_from_song_info(song)
        self.assertEqual(tags, {"title": "A Title"})
        self.assertEqual(cover, "")

    def test_a_source_with_no_catalogue_entry_still_tags_what_it_has(self):
        song = _SongInfo(song_name="A Title", singers="Someone",
                         raw_data={"search": {"unrelated": "shape"}})
        tags, _cover = music_tags.tags_from_song_info(song)
        self.assertEqual(tags["title"], "A Title")
        self.assertEqual(tags["artist"], "Someone")

    def test_a_unix_timestamp_is_not_mistaken_for_a_release_date(self):
        """Qobuz's released_at is seconds, not a date: it would read as 1366."""
        album = dict(QOBUZ_ITEM["album"], released_at=1366322400)
        album.pop("release_date_original")
        song = _SongInfo(raw_data={"search": dict(QOBUZ_ITEM, album=album)})
        tags, _cover = music_tags.tags_from_song_info(song)
        self.assertNotIn("date", tags)

    def test_a_reissues_stream_date_is_used_only_without_the_original(self):
        album = dict(QOBUZ_ITEM["album"], release_date_stream="2021-01-01")
        album.pop("release_date_original")
        song = _SongInfo(raw_data={"search": dict(QOBUZ_ITEM, album=album)})
        tags, _cover = music_tags.tags_from_song_info(song)
        self.assertEqual(tags["date"], "2021-01-01")

    def test_track_zero_is_not_a_track_number(self):
        item = dict(QOBUZ_ITEM, track_number=0)
        song = _SongInfo(raw_data={"search": item})
        tags, _cover = music_tags.tags_from_song_info(song)
        self.assertNotIn("tracknumber", tags)


class WritingTagsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blinddl-tags-")

    def test_a_flac_gets_every_field_the_catalogue_knew(self):
        path = _flac_path(self.dir)
        tags = {"title": "A Title", "artist": "A Performer",
                "album": "An Album", "albumartist": "An Album Artist",
                "date": "2013-05-17", "tracknumber": "3", "tracktotal": "13",
                "discnumber": "1", "genre": "Electronic",
                "isrc": "GBAYE0601498", "label": "A Label",
                "musicbrainz_trackid": "rec-id"}
        written = music_tags.write_tags(path, tags)
        self.assertGreaterEqual(written, 12)
        saved = FLAC(path)
        self.assertEqual(saved["title"], ["A Title"])
        self.assertEqual(saved["albumartist"], ["An Album Artist"])
        self.assertEqual(saved["tracknumber"], ["3"])
        self.assertEqual(saved["tracktotal"], ["13"])
        self.assertEqual(saved["isrc"], ["GBAYE0601498"])
        self.assertEqual(saved["musicbrainz_trackid"], ["rec-id"])
        # Players that read only a bare year get one.
        self.assertEqual(saved["year"], ["2013"])

    def test_a_tag_the_file_already_carries_is_left_alone(self):
        """A file that arrived tagged knows itself better than a search does."""
        path = _flac_path(self.dir)
        first = FLAC(path)
        first["TITLE"] = ["What The File Says"]
        first.save()
        music_tags.write_tags(path, {"title": "What The Search Guessed",
                                     "album": "An Album"})
        saved = FLAC(path)
        self.assertEqual(saved["title"], ["What The File Says"])
        self.assertEqual(saved["album"], ["An Album"])

    def test_cover_art_is_embedded_once(self):
        path = _flac_path(self.dir)
        cover = b"\xff\xd8\xff" + b"0" * 64
        music_tags.write_tags(path, {"title": "A Title"}, cover, "image/jpeg")
        self.assertEqual(len(FLAC(path).pictures), 1)
        # A second pass must not stack a duplicate picture on the file.
        music_tags.write_tags(path, {"title": "A Title"}, cover, "image/jpeg")
        self.assertEqual(len(FLAC(path).pictures), 1)

    def test_an_mp3_gets_id3_frames_including_track_of_total(self):
        path = _mp3_path(self.dir)
        tags = {"title": "A Title", "artist": "A Performer",
                "album": "An Album", "albumartist": "An Album Artist",
                "tracknumber": "3", "tracktotal": "13", "discnumber": "1",
                "isrc": "GBAYE0601498", "label": "A Label",
                "musicbrainz_albumid": "album-id"}
        music_tags.write_tags(path, tags, b"\xff\xd8\xff" + b"0" * 64)
        saved = ID3(path)
        self.assertEqual(saved.getall("TIT2")[0].text, ["A Title"])
        self.assertEqual(saved.getall("TPE2")[0].text, ["An Album Artist"])
        self.assertEqual(saved.getall("TRCK")[0].text, ["3/13"])
        self.assertEqual(saved.getall("TPOS")[0].text, ["1"])
        self.assertEqual(saved.getall("TSRC")[0].text, ["GBAYE0601498"])
        self.assertEqual(len(saved.getall("APIC")), 1)
        mbid = [frame for frame in saved.getall("TXXX")
                if frame.desc == "MusicBrainz Album Id"]
        self.assertEqual(mbid[0].text, ["album-id"])

    def test_a_format_mutagen_cannot_read_is_left_untouched(self):
        path = os.path.join(self.dir, "not-audio.bin")
        with open(path, "wb") as handle:
            handle.write(b"nothing to see here")
        self.assertEqual(music_tags.write_tags(path, {"title": "A Title"}), 0)


class MusicBrainzTests(unittest.TestCase):
    ISRC_ANSWER = {
        "recordings": [{
            "id": "rec-id",
            "title": "A Title",
            "isrcs": ["GBAYE0601498"],
            "artist-credit": [{"name": "A Performer",
                               "artist": {"id": "artist-id",
                                          "name": "A Performer"}}],
            "releases": [{
                "id": "release-id",
                "title": "An Album",
                "date": "2013-05-17",
                "release-group": {"id": "group-id",
                                  "first-release-date": "2013-05-17"},
                "media": [{"track-count": 13, "track": [{"number": "3"}]}],
            }],
        }]
    }

    def test_an_isrc_is_looked_up_as_an_identifier_not_a_search(self):
        """An ISRC names one recording, so it is asked for before any search."""
        with mock.patch.object(music_tags, "_mb_get",
                               return_value=self.ISRC_ANSWER) as get:
            tags = {"isrc": "GBAYE0601498", "title": "A Title"}
            music_tags.lookup_musicbrainz(tags)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args[0][0], "isrc/GBAYE0601498")
        self.assertEqual(tags["musicbrainz_trackid"], "rec-id")
        self.assertEqual(tags["musicbrainz_albumid"], "release-id")
        self.assertEqual(tags["musicbrainz_artistid"], "artist-id")
        self.assertEqual(tags["musicbrainz_releasegroupid"], "group-id")
        self.assertEqual(tags["album"], "An Album")
        self.assertEqual(tags["date"], "2013-05-17")
        self.assertEqual(tags["tracknumber"], "3")

    def test_a_collaboration_keeps_the_spacing_of_its_credit(self):
        """The join phrase carries its own spaces and must not be trimmed."""
        credits = [
            {"name": "Daft Punk", "artist": {"id": "a", "name": "Daft Punk"},
             "joinphrase": " feat. "},
            {"name": "Pharrell Williams",
             "artist": {"id": "b", "name": "Pharrell Williams"}},
        ]
        self.assertEqual(music_tags._credited_artist(credits),
                         "Daft Punk feat. Pharrell Williams")

    def test_an_inferred_match_never_names_the_album(self):
        """A cover on a compilation scores as highly as the original."""
        recording = dict(self.ISRC_ANSWER["recordings"][0], score=97)
        recording["releases"] = [{"id": "comp-id",
                                  "title": "Undercover, Vol. 2"}]
        with mock.patch.object(music_tags, "_mb_get",
                               return_value={"recordings": [recording]}):
            tags = {"title": "A Title", "artist": "A Performer"}
            music_tags.lookup_musicbrainz(tags)
        # The identifiers are still worth having; the album is not.
        self.assertEqual(tags["musicbrainz_trackid"], "rec-id")
        self.assertNotIn("album", tags)
        self.assertNotIn("musicbrainz_albumid", tags)
        self.assertNotIn("tracknumber", tags)

    def test_an_isrc_match_may_name_the_album(self):
        with mock.patch.object(music_tags, "_mb_get",
                               return_value=self.ISRC_ANSWER):
            tags = {"isrc": "GBAYE0601498"}
            music_tags.lookup_musicbrainz(tags)
        self.assertEqual(tags["album"], "An Album")

    def test_the_isrc_resource_is_not_asked_for_release_groups(self):
        """MusicBrainz answers 400, and a 400 costs every tag on this path."""
        with mock.patch.object(music_tags, "_mb_get",
                               return_value=self.ISRC_ANSWER) as get:
            music_tags.lookup_musicbrainz({"isrc": "GBAYE0601498"})
        self.assertEqual(get.call_args[0][1]["inc"],
                         "artist-credits+releases")

    def test_a_covers_compilation_is_not_taken_for_the_album(self):
        """The first release listed is routinely not the one to tag with."""
        releases = [
            {"title": "Undercover, Vol. 2", "date": "2014-01-01",
             "status": "Official",
             "release-group": {"primary-type": "Album",
                               "secondary-types": ["Compilation"]}},
            {"title": "The Original Album", "date": "2013-05-17",
             "status": "Official",
             "release-group": {"primary-type": "Album",
                               "secondary-types": []}},
        ]
        self.assertEqual(music_tags._best_release(releases)["title"],
                         "The Original Album")

    def test_the_earliest_official_release_wins(self):
        releases = [
            {"title": "A Reissue", "date": "2019-01-01", "status": "Official",
             "release-group": {"primary-type": "Album"}},
            {"title": "A Bootleg", "date": "2012-01-01",
             "status": "Bootleg", "release-group": {"primary-type": "Album"}},
            {"title": "The Original", "date": "2013-05-17",
             "status": "Official", "release-group": {"primary-type": "Album"}},
        ]
        self.assertEqual(music_tags._best_release(releases)["title"],
                         "The Original")

    def test_a_release_with_no_date_does_not_outrank_a_dated_one(self):
        releases = [
            {"title": "Undated", "status": "Official",
             "release-group": {"primary-type": "Album"}},
            {"title": "Dated", "date": "2013-05-17", "status": "Official",
             "release-group": {"primary-type": "Album"}},
        ]
        self.assertEqual(music_tags._best_release(releases)["title"], "Dated")

    def test_a_weak_name_match_is_refused(self):
        """Wrong tags are worse than none, so a low-scoring guess is dropped."""
        answer = {"recordings": [dict(self.ISRC_ANSWER["recordings"][0],
                                      score=42)]}
        with mock.patch.object(music_tags, "_mb_get", return_value=answer):
            tags = {"title": "A Title", "artist": "A Performer"}
            music_tags.lookup_musicbrainz(tags)
        self.assertNotIn("musicbrainz_trackid", tags)

    def test_a_confident_name_match_is_taken(self):
        answer = {"recordings": [dict(self.ISRC_ANSWER["recordings"][0],
                                      score=97)]}
        with mock.patch.object(music_tags, "_mb_get",
                               return_value=answer) as get:
            tags = {"title": "A Title", "artist": "A Performer"}
            music_tags.lookup_musicbrainz(tags)
        self.assertEqual(get.call_args[0][0], "recording")
        self.assertEqual(tags["musicbrainz_trackid"], "rec-id")

    def test_what_the_site_already_said_is_not_replaced(self):
        with mock.patch.object(music_tags, "_mb_get",
                               return_value=self.ISRC_ANSWER):
            tags = {"isrc": "GBAYE0601498", "album": "The Deluxe Edition"}
            music_tags.lookup_musicbrainz(tags)
        self.assertEqual(tags["album"], "The Deluxe Edition")

    def test_a_query_with_special_characters_is_escaped(self):
        with mock.patch.object(music_tags, "_mb_get",
                               return_value={}) as get:
            music_tags.lookup_musicbrainz({"title": 'A "Quoted" Title: Part 1'})
        query = get.call_args[0][1]["query"]
        self.assertIn('\\"Quoted\\"', query)
        self.assertIn("\\:", query)

    def test_lookups_are_held_to_one_a_second_across_threads(self):
        """MusicBrainz rate limits the application, not the connection."""
        music_tags._mb_last_call = 0.0
        with mock.patch.object(music_tags.requests, "get") as get, \
                mock.patch.object(music_tags.time, "sleep") as sleep:
            get.return_value = mock.Mock(
                json=lambda: {}, raise_for_status=lambda: None)
            music_tags._mb_get("recording", {})
            music_tags._mb_get("recording", {})
        self.assertTrue(sleep.called)
        self.assertGreater(sleep.call_args[0][0], 0)

    def test_a_service_that_is_down_simply_adds_nothing(self):
        with mock.patch.object(music_tags.requests, "get",
                               side_effect=OSError("no route")):
            self.assertIsNone(music_tags._mb_get("recording", {}))

    def test_the_user_agent_names_the_application(self):
        """Anonymous clients get blocked, so the header is not optional."""
        self.assertIn("blindDL", music_tags._USER_AGENT)
        self.assertIn("github.com/serrebidev/blindDL", music_tags._USER_AGENT)


class ITunesTests(unittest.TestCase):
    ANSWER = {"results": [{
        "trackName": "A Title",
        "artistName": "A Performer",
        "collectionName": "An Album",
        "collectionArtistName": "An Album Artist",
        "primaryGenreName": "Electronic",
        "releaseDate": "2013-05-17T07:00:00Z",
        "trackNumber": 3, "trackCount": 13,
        "discNumber": 1, "discCount": 2,
        "artworkUrl100": "https://example.invalid/a/100x100bb.jpg",
    }]}

    def _response(self, body):
        return mock.Mock(raise_for_status=lambda: None, json=lambda: body)

    def test_apple_fills_the_record_an_identifier_lookup_does_not(self):
        with mock.patch.object(music_tags.requests, "get",
                               return_value=self._response(self.ANSWER)):
            tags = {"title": "A Title", "artist": "A Performer"}
            cover = music_tags.lookup_itunes(tags)
        self.assertEqual(tags["album"], "An Album")
        self.assertEqual(tags["albumartist"], "An Album Artist")
        self.assertEqual(tags["genre"], "Electronic")
        self.assertEqual(tags["date"], "2013-05-17")
        self.assertEqual(tags["tracknumber"], "3")
        self.assertEqual(tags["tracktotal"], "13")
        self.assertEqual(tags["disctotal"], "2")
        # The thumbnail Apple returns is upgraded to a size worth embedding.
        self.assertEqual(cover, "https://example.invalid/a/600x600bb.jpg")

    def test_a_different_song_that_merely_searches_alike_is_refused(self):
        answer = {"results": [{
            "trackName": "Something Else Entirely",
            "artistName": "A Different Band",
            "collectionName": "Another Record",
            "primaryGenreName": "Metal",
        }]}
        with mock.patch.object(music_tags.requests, "get",
                               return_value=self._response(answer)):
            tags = {"title": "A Title", "artist": "A Performer"}
            cover = music_tags.lookup_itunes(tags)
        self.assertEqual(cover, "")
        self.assertNotIn("genre", tags)
        self.assertNotIn("album", tags)

    def test_the_best_of_several_candidates_wins(self):
        answer = {"results": [
            {"trackName": "Wrong Song", "artistName": "Someone Else",
             "collectionName": "Wrong Record"},
            self.ANSWER["results"][0],
        ]}
        with mock.patch.object(music_tags.requests, "get",
                               return_value=self._response(answer)):
            tags = {"title": "A Title", "artist": "A Performer"}
            music_tags.lookup_itunes(tags)
        self.assertEqual(tags["album"], "An Album")

    def test_a_service_that_is_down_adds_nothing(self):
        with mock.patch.object(music_tags.requests, "get",
                               side_effect=OSError("no route")):
            tags = {"title": "A Title", "artist": "A Performer"}
            self.assertEqual(music_tags.lookup_itunes(tags), "")
        self.assertEqual(tags, {"title": "A Title", "artist": "A Performer"})


class LyricsTests(unittest.TestCase):
    # Stand-in text: the shape of an LRC line, with no song's words in it.
    LRC = "[00:01.00] first line\n[00:05.00] second line"

    def test_synced_words_are_preferred_over_plain_ones(self):
        body = {"syncedLyrics": self.LRC, "plainLyrics": "first line"}
        with mock.patch.object(music_tags.requests, "get",
                               return_value=mock.Mock(status_code=200,
                                                      json=lambda: body)):
            self.assertEqual(
                music_tags.fetch_lyrics("A Title", "A Performer"), self.LRC)

    def test_the_running_time_is_sent_so_the_right_take_is_matched(self):
        """Two recordings of one song are told apart by how long they run."""
        with mock.patch.object(music_tags.requests, "get",
                               return_value=mock.Mock(
                                   status_code=200,
                                   json=lambda: {})) as get:
            music_tags.fetch_lyrics("A Title", "A Performer", "An Album", 245)
        self.assertEqual(get.call_args[1]["params"]["duration"], 245)

    def test_a_track_with_no_words_on_file_is_not_an_error(self):
        with mock.patch.object(music_tags.requests, "get",
                               return_value=mock.Mock(status_code=404)):
            self.assertEqual(music_tags.fetch_lyrics("A Title", "A"), "")

    def test_words_reach_a_flac_and_an_mp3(self):
        directory = tempfile.mkdtemp(prefix="blinddl-lyrics-")
        flac = _flac_path(directory)
        music_tags.write_tags(flac, {"title": "A Title", "lyrics": self.LRC})
        self.assertEqual(FLAC(flac)["lyrics"], [self.LRC])

        mp3 = _mp3_path(directory)
        music_tags.write_tags(mp3, {"title": "A Title", "lyrics": self.LRC})
        self.assertEqual(ID3(mp3).getall("USLT")[0].text, self.LRC)

    def test_deezer_asks_the_same_service_the_same_way(self):
        from blinddl import deezer_backend

        with mock.patch.object(music_tags, "fetch_lyrics",
                               return_value=self.LRC) as fetch:
            result = deezer_backend._fetch_lrclib_lyrics({
                "SNG_TITLE": "A Title", "ART_NAME": "A Performer",
                "ALB_TITLE": "An Album", "DURATION": 245})
        self.assertEqual(result, self.LRC)
        self.assertEqual(fetch.call_args[0],
                         ("A Title", "A Performer", "An Album", 245))

    def test_nothing_found_stays_none_for_deezer(self):
        from blinddl import deezer_backend

        with mock.patch.object(music_tags, "fetch_lyrics", return_value=""):
            self.assertIsNone(
                deezer_backend._fetch_lrclib_lyrics({"SNG_TITLE": "A"}))


class TheAudioDBTests(unittest.TestCase):
    def test_an_album_fills_genre_year_and_artwork(self):
        answer = {"album": [{"strGenre": "House",
                             "intYearReleased": "2013",
                             "strArtist": "An Album Artist",
                             "strLabel": "A Label",
                             "strAlbumThumb": "https://example.invalid/a.jpg"}]}
        with mock.patch.object(music_tags, "_audiodb_get",
                               return_value=answer) as get:
            tags = {"artist": "A Performer", "album": "An Album"}
            cover = music_tags.lookup_theaudiodb(tags)
        self.assertEqual(get.call_args[0][0], "searchalbum.php")
        self.assertEqual(tags["genre"], "House")
        self.assertEqual(tags["date"], "2013")
        self.assertEqual(cover, "https://example.invalid/a.jpg")

    def test_with_no_album_the_track_is_searched_instead(self):
        answer = {"track": [{"strGenre": "House", "strAlbum": "An Album",
                             "intTrackNumber": "3"}]}
        with mock.patch.object(music_tags, "_audiodb_get",
                               return_value=answer) as get:
            tags = {"artist": "A Performer", "title": "A Title"}
            music_tags.lookup_theaudiodb(tags)
        self.assertEqual(get.call_args[0][0], "searchtrack.php")
        self.assertEqual(tags["album"], "An Album")
        self.assertEqual(tags["tracknumber"], "3")

    def test_without_an_artist_there_is_nothing_to_ask(self):
        with mock.patch.object(music_tags, "_audiodb_get") as get:
            self.assertEqual(music_tags.lookup_theaudiodb({"title": "A"}), "")
        get.assert_not_called()


class CoverFetchTests(unittest.TestCase):
    def _response(self, body, length=None):
        return mock.Mock(
            raise_for_status=lambda: None,
            headers={"Content-Length": str(length if length is not None
                                           else len(body))},
            iter_content=lambda chunk_size: [body],
            __enter__=lambda self_: self_,
            __exit__=lambda *args: False,
        )

    def test_a_jpeg_is_recognised(self):
        body = b"\xff\xd8\xff" + b"0" * 32
        with mock.patch.object(music_tags.requests, "get",
                               return_value=self._response(body)):
            data, mime = music_tags.fetch_cover("https://example.invalid/a.jpg")
        self.assertEqual(data, body)
        self.assertEqual(mime, "image/jpeg")

    def test_something_that_is_not_an_image_is_refused(self):
        with mock.patch.object(music_tags.requests, "get",
                               return_value=self._response(b"<html>")):
            data, mime = music_tags.fetch_cover("https://example.invalid/a.jpg")
        self.assertIsNone(data)
        self.assertEqual(mime, "")

    def test_an_oversized_master_is_not_embedded(self):
        with mock.patch.object(music_tags.requests, "get",
                               return_value=self._response(
                                   b"\xff\xd8\xff",
                                   length=music_tags.MAX_COVER_BYTES + 1)):
            data, _mime = music_tags.fetch_cover("https://example.invalid/a.jpg")
        self.assertIsNone(data)


class TagDownloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blinddl-tagdl-")

    def test_a_finished_qobuz_download_ends_up_filed(self):
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", singers="A Performer",
                         album="An Album", raw_data={"search": QOBUZ_ITEM})
        with mock.patch.object(music_tags, "lookup_musicbrainz") as mb, \
                mock.patch.object(music_tags, "lookup_theaudiodb",
                                  return_value="") as adb, \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")):
            written = music_tags.tag_download(path, song)
        self.assertTrue(mb.called and adb.called)
        self.assertGreater(written, 0)
        saved = FLAC(path)
        self.assertEqual(saved["albumartist"], ["An Album Artist"])
        self.assertEqual(saved["tracknumber"], ["3"])

    def test_a_sleeve_from_the_site_does_not_skip_the_other_lookups(self):
        """The thin sources are the ones that need Apple: they send a title,
        an artist and a picture, and nothing else. Hanging the lookups off
        whether a cover was still wanted skipped them for exactly those."""
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", singers="A Performer",
                         cover_url="https://example.invalid/site.jpg")

        def add_the_record(tags):
            tags["album"] = "From Apple"
            tags["genre"] = "Electronic"
            return "https://example.invalid/apple.jpg"

        with mock.patch.object(music_tags, "lookup_musicbrainz"), \
                mock.patch.object(music_tags, "lookup_itunes",
                                  side_effect=add_the_record) as itunes, \
                mock.patch.object(music_tags, "fetch_lyrics",
                                  return_value=""), \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")) as cover:
            music_tags.tag_download(path, song)

        itunes.assert_called_once()
        self.assertEqual(FLAC(path)["album"], ["From Apple"])
        # ...and the site's own sleeve is still the one embedded.
        self.assertEqual(cover.call_args[0][0],
                         "https://example.invalid/site.jpg")

    def test_a_row_that_already_knows_everything_asks_no_one(self):
        """A Qobuz row arrives complete, so both catalogue lookups would be
        round trips that can only confirm what is already on the file."""
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", raw_data={"search": QOBUZ_ITEM})

        def fill_the_rest(tags):
            for field in music_tags._AUDIODB_FILLS:
                tags[field] = tags.get(field) or "known"

        with mock.patch.object(music_tags, "lookup_musicbrainz",
                               side_effect=fill_the_rest), \
                mock.patch.object(music_tags, "lookup_itunes") as itunes, \
                mock.patch.object(music_tags, "lookup_theaudiodb") as adb, \
                mock.patch.object(music_tags, "fetch_lyrics",
                                  return_value=""), \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")):
            music_tags.tag_download(path, song)

        itunes.assert_not_called()
        adb.assert_not_called()

    def test_a_thin_row_is_taken_to_both_catalogues(self):
        """zvu4it and the like send a title and an artist and no more."""
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", singers="A Performer")
        with mock.patch.object(music_tags, "lookup_musicbrainz"), \
                mock.patch.object(music_tags, "lookup_itunes",
                                  return_value="") as itunes, \
                mock.patch.object(music_tags, "lookup_theaudiodb",
                                  return_value="") as adb, \
                mock.patch.object(music_tags, "fetch_lyrics",
                                  return_value=""), \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")):
            music_tags.tag_download(path, song)
        itunes.assert_called_once()
        adb.assert_called_once()

    def test_words_are_looked_up_and_written(self):
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", raw_data={"search": QOBUZ_ITEM})
        with mock.patch.object(music_tags, "lookup_musicbrainz"), \
                mock.patch.object(music_tags, "lookup_itunes",
                                  return_value=""), \
                mock.patch.object(music_tags, "lookup_theaudiodb",
                                  return_value=""), \
                mock.patch.object(music_tags, "fetch_lyrics",
                                  return_value="[00:01.00] a line") as words, \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")):
            music_tags.tag_download(path, song)
        words.assert_called_once()
        self.assertEqual(FLAC(path)["lyrics"], ["[00:01.00] a line"])

    def test_words_the_site_sent_are_not_looked_up_again(self):
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", raw_data={"search": QOBUZ_ITEM})
        song.lyric = "[00:02.00] the site's own"
        with mock.patch.object(music_tags, "lookup_musicbrainz"), \
                mock.patch.object(music_tags, "lookup_itunes",
                                  return_value=""), \
                mock.patch.object(music_tags, "lookup_theaudiodb",
                                  return_value=""), \
                mock.patch.object(music_tags, "fetch_lyrics") as words, \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")):
            music_tags.tag_download(path, song)
        words.assert_not_called()
        self.assertEqual(FLAC(path)["lyrics"], ["[00:02.00] the site's own"])

    def test_the_lookup_can_be_switched_off(self):
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", raw_data={"search": QOBUZ_ITEM})
        with mock.patch.object(music_tags, "lookup_musicbrainz") as mb, \
                mock.patch.object(music_tags, "lookup_theaudiodb") as adb, \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")):
            music_tags.tag_download(path, song, online=False)
        mb.assert_not_called()
        adb.assert_not_called()
        # The site's own metadata is still written; that costs nothing.
        self.assertEqual(FLAC(path)["album"], ["An Album"])

    def test_a_service_that_throws_never_costs_the_download(self):
        path = _flac_path(self.dir)
        song = _SongInfo(song_name="A Title", raw_data={"search": QOBUZ_ITEM})
        with mock.patch.object(music_tags, "lookup_musicbrainz",
                               side_effect=RuntimeError("down")), \
                mock.patch.object(music_tags, "lookup_theaudiodb",
                                  side_effect=RuntimeError("down")), \
                mock.patch.object(music_tags, "fetch_cover",
                                  return_value=(None, "")):
            written = music_tags.tag_download(path, song)
        self.assertGreater(written, 0)
        self.assertEqual(FLAC(path)["title"], ["A Title"])

    def test_a_file_that_is_not_there_is_not_an_error(self):
        missing = os.path.join(self.dir, "gone.flac")
        self.assertEqual(music_tags.tag_download(missing, _SongInfo()), 0)
        self.assertEqual(music_tags.tag_download("", _SongInfo()), 0)


class DownloadPathTests(unittest.TestCase):
    def test_every_musicdl_download_is_tagged_on_its_way_out(self):
        from blinddl import musicdl_backend

        song = _SongInfo(song_name="A Title")
        done = mock.Mock(save_path="C:\\music\\a.flac")
        client = mock.Mock(download=mock.Mock(return_value=[done]))
        with mock.patch.object(musicdl_backend, "_get_clients",
                               return_value={song.source: client}), \
                mock.patch.object(musicdl_backend.music_tags,
                                  "tag_download") as tag:
            musicdl_backend.download(song, tempfile.mkdtemp())
        tag.assert_called_once()
        self.assertEqual(tag.call_args[0][0], "C:\\music\\a.flac")
        self.assertIs(tag.call_args[1]["online"], True)

    def test_the_setting_reaches_the_tagger(self):
        from blinddl import musicdl_backend

        song = _SongInfo(song_name="A Title")
        done = mock.Mock(save_path="C:\\music\\a.flac")
        client = mock.Mock(download=mock.Mock(return_value=[done]))
        with mock.patch.object(musicdl_backend, "_get_clients",
                               return_value={song.source: client}), \
                mock.patch.object(musicdl_backend.music_tags,
                                  "tag_download") as tag:
            musicdl_backend.download(song, tempfile.mkdtemp(),
                                     online_lookup=False)
        self.assertIs(tag.call_args[1]["online"], False)


if __name__ == "__main__":
    unittest.main()
