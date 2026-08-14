"""Side B's providers.

The audio provider is fetched on demand rather than imported here. Importing
it pulls in yt-dlp and ytmusicapi -- about half a second of processor time
on a warm cache, more on a cold one -- and a metadata-only search, which is
what blindDL's music search runs, never touches either of them. Importing
``sideb.providers.metadata.deezer`` runs this file first, so an eager import
here was charged to every search.
"""

__all__ = ["AudioProvider", "YouTubeAudio", "is_instrumental"]


def __getattr__(name):
    """Import the audio provider the first time one of its names is used."""
    if name in __all__:
        from sideb.providers.audio.base import AudioProvider
        from sideb.providers.audio.youtube import YouTubeAudio, is_instrumental

        globals().update(
            AudioProvider=AudioProvider,
            YouTubeAudio=YouTubeAudio,
            is_instrumental=is_instrumental,
        )
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
