# Optional and account-based sites

This page holds the setup detail that would otherwise clutter the README. None
of it is needed for ordinary downloads, music, books, or subscriptions.

## Adult sites

Adult support is installed with the normal dependencies and in packaged
releases, but it is **disabled by default**. Turning on **Enable adult sites**
in Settings adds the adult categories to the Search source combo box; the
Search sites dialog then picks which of them are queried.

- Category searches filter out results whose metadata belongs to another
  category, so a category search stays on topic even where a site mixes them.
- Some sites are URL-download only, because they have no usable search or their
  search pages block automated requests. Paste a link for those.
- Playlist-style URLs expand into individual queue items.
- Pages that require an account work through **Use cookies from browser** in
  Settings, which lets yt-dlp read a browser profile you are already signed into.
- Adult API libraries keep their upstream licenses and need Python 3.12 or newer.

## Creator sites with an authentication file

A few creator platforms take a JSON file you point at in Settings. BlindDL
stores only the path to that file, never a copy of its contents. Treat these
files like passwords.

OnlyFans uses the `ofd`-compatible non-DRM fields:

```json
{
  "cookie": "auth_id=YOUR_ID; sess=YOUR_SESSION",
  "x_bc": "YOUR_X_BC_HEADER",
  "user_agent": "THE_MATCHING_BROWSER_USER_AGENT"
}
```

JustForFans uses the cookie and account ID visible on an authenticated
`ajax/getPosts.php` browser request:

```json
{
  "cookie": "userhash4=YOUR_HASH; OTHER_SESSION_COOKIES",
  "user_id": "YOUR_NUMERIC_ACCOUNT_ID",
  "user_agent": "YOUR_BROWSER_USER_AGENT"
}
```

Only ordinary MP4, HLS, image, audio, and GIF media is supported. BlindDL does
not accept DRM device keys and does not decrypt protected media.

## Anna's Archive

Anna's Archive is an index rather than a file server, and its own free download
sits behind a browser check that returns 403 to any application. BlindDL
resolves a record through the public LibGen mirrors instead, and prefers records
LibGen actually holds. If you have a membership, put its key in Settings to use
the fast partner servers. When neither can serve a file, BlindDL says so and
Ctrl+C copies the record's URL so you can use the site's own slow download.
