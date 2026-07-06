# Mimicry

> Personal-use tool. Not affiliated with YouTube or Google. Only download
> content you have the right to download — respect copyright and YouTube's
> Terms of Service.

> ⚠️ **Vibe-coded.** Built rapidly with heavy AI assistance rather than
> hand-audited line by line. It works for my own use, but hasn't seen
> extensive testing across different OSes, playlist sizes, or edge cases.
> Expect occasional bugs; check "Known issues" below before assuming
> something's broken on your end, and feel free to open an issue if you
> hit something not listed there.

Paste a link — YouTube, SoundCloud, Twitter/X, Reddit, Vimeo, Twitch, TikTok,
Bandcamp, and a handful of others — see thumbnails and titles, pick what you
want, and download at the highest available quality — with cover art, correct
release year, and lyrics embedded automatically.

Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) under the hood, which
supports 1000+ sites — the sites above are the ones this app recognizes well
enough to show a proper name/badge for; anything else yt-dlp can reach will
still generally download fine, just under a generic "web" badge.

## Advantages
- **No CLI required** — a full browser UI over yt-dlp, so you get its site
  coverage without memorizing flags or reading yt-dlp's own docs.
- **Metadata done properly** — cover art, the song's real release year (not
  just the video's upload date), and optional lyrics get embedded directly
  into the file's own tags automatically, not left as a separate step.
- **Duplicate-aware** — catches near-identical titles both within a single
  playlist and across your whole existing library, not just exact re-downloads.
- **Skips what you already have** — Compare and "Download missing only"
  check your library by an embedded ID tag (not filename), so a renamed or
  re-tagged file still doesn't get downloaded twice.
- **Locked down by default** — binds to localhost only and requires an
  app token for every API call; most small local tools like this assume
  nobody else is on your network.
- **Self-hosted and private** — nothing leaves your machine except requests
  to the site you're downloading from and lrclib.net for lyrics lookups; no
  telemetry, no account, no cloud processing of your files.
- **Portable, not "installed"** — no installer, no system service, no
  registry/system-wide changes. It's a Python script plus a handful of pip
  packages; if a machine already has Python, pip, and ffmpeg, running it is
  just `pip install -r requirements.txt` and `python3 app.py`. Move the
  folder to another machine with those same pieces already present and it
  runs the same way — nothing to "uninstall" beyond deleting the folder.

## Features
- **Thumbnails + titles** pulled straight from the playlist, no download needed just to browse it. Playlist lookups are cached briefly, so re-checking the same playlist (compare, missing-check, re-fetch) doesn't keep re-hitting YouTube.
- **Search YouTube directly** from the same queue, not just from a playlist link. (Search is YouTube-only — other sites need a direct link pasted in.)
- **Multi-site support** — YouTube, SoundCloud, Twitter/X, Reddit, Vimeo, Twitch, TikTok, Bandcamp, Instagram, Facebook, Dailymotion, and Bilibili are recognized with proper site badges; anything else yt-dlp supports still downloads, just under a generic badge. A few of these (TikTok, Instagram, Facebook, private Vimeo links, Twitch subscriber VODs, age-restricted Dailymotion) need a logged-in session to work reliably — the app shows a heads-up in the UI when your pasted link matches one of these.
- **Already-downloaded detection** — matched by a YouTube video ID tag embedded in each file (not the filename), so titles stay clean. Old files from earlier versions (with `[id]` in the filename) are still recognized.
- **Within-playlist duplicate detection** — flags near-identical titles (e.g. "Song (Official Video)" vs "Song (Audio)"), with a fuzzy matcher that ignores channel-wide boilerplate text so it doesn't over-match.
- **Compare view** — every playlist track side-by-side with what's already on disk, with downloaded/missing/duplicate counts.
- **Download missing only** — re-checks a playlist against your library and grabs just what's new.
- **Formats**: best available (no re-encode), FLAC, WAV, MP3 (configurable quality in preferences: best VBR or a fixed 128/192/256/320 kbps — plus a quick "MP3 — small" option in the main dropdown that's always a fixed 128kbps regardless of that preference), M4A, or video.
- **Video options** — resolution cap (best/1080p/720p/480p), container (MP4/MKV/WebM), and an optional compression pass (light/strong) for when the resolution cap alone isn't enough to shrink a file.
- **Retries on failure** — transient failures (a network blip, a brief rate-limit, a file lock) automatically retry with backoff before a track is marked as an error; the number of retries is configurable in preferences (0–5).
- **Cover art** — YouTube's thumbnail, cropped to a clean square, embedded directly into the file's own tags (mp3/m4a/flac/wav).
- **Correct release year** — replaces the tag ffmpeg sets from the video's *upload* date (often years off from the actual song) with the real release year when YouTube Music metadata has one, or leaves it blank rather than showing a wrong year.
- **Lyrics (optional)** — looks up real lyrics from [lrclib.net](https://lrclib.net) first (synced when available), falling back to YouTube's captions (manual if the video has them, otherwise auto-generated) only if nothing was found. Off by default — turn it on in the preferences row in the UI.
- **Configurable concurrency** — how many tracks download at once (1–6), adjustable in the UI.
- **Instant preview** — streams audio or video straight from the source site with no local caching of the media itself; the resolved stream link is cached briefly so replaying/scrubbing the same track comes back instantly instead of re-resolving it. Already-downloaded tracks preview from the real file directly.
- **Library browser** — search, play, and delete anything you've already downloaded, right from the app. Also flags duplicate songs sitting in your library under different filenames (e.g. downloaded twice from two different playlists).
- **M3U export** — writes a `.m3u` file into your download folder listing a playlist's downloaded tracks in their original order.
- **Folder picker** — choose your download folder via a native OS dialog instead of typing a path.
- **Standalone `/compare` page** — a lightweight comparison view that works independently of the main UI.
- **Locked down by default** — every `/api/*` route requires an auto-generated app token (see "Security" below), and the server only listens on `127.0.0.1` unless you explicitly opt into LAN access.

## Project layout
```
mimicry/
├── app.py
├── requirements.txt
├── templates/
│   └── index.html      # Flask loads this via render_template — must stay here
├── README.md
├── LICENSE
└── .gitignore
```
`settings.json` and `cookies.txt` (if you create one) are generated/placed
next to `app.py` at runtime — they're git-ignored and shouldn't be committed.

## Setup
```bash
pip install -r requirements.txt --break-system-packages
```
You also need **ffmpeg** installed and on your PATH (required for audio extraction/conversion and video merging):
- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: download from ffmpeg.org and add its `bin` folder to PATH

The folder picker uses **tkinter**, which ships with Python on macOS and
Windows but is sometimes a separate package on Linux:
- Ubuntu/Debian: `sudo apt install python3-tk`

If tkinter isn't available, the folder picker button will show an error —
you can still set your download folder by typing the path directly.

## Run
```bash
python3 app.py
```
Open **http://127.0.0.1:5000**.

Downloads are saved to `~/Music/YT Playlist Downloads` by default (your own
user folder — no admin rights needed). Change it anytime from the folder
picker in the app, or by editing `DEFAULT_DOWNLOAD_DIR` at the top of
`app.py`. Once set, your choice is remembered in `settings.json` next to the
app.

## Security

By default the server only listens on `127.0.0.1` — nothing outside your
own machine can reach it. Every `/api/*` route also requires an app token
that's generated automatically on first run and stored in `settings.json`;
your own browser gets it from the page automatically, so there's nothing to
configure. **Don't share or commit `settings.json`** — the token in it is
equivalent to full control of the app (triggering downloads, browsing or
deleting your library, changing your download folder) for anyone who has it
and can reach the server.

Two environment variables change this behavior:
- `YTPLD_DEBUG=1` — turns on Flask's debug mode (auto-reload + the Werkzeug
  debugger) for local development. Leave unset in normal use — the debugger
  allows arbitrary code execution if it's ever reachable beyond localhost.
- `MIMICRY_ALLOW_LAN=1` — binds to `0.0.0.0` instead of `127.0.0.1`, so
  other devices on your network (e.g. your phone) can reach it. The app
  token is still required, but only do this on a network you trust — the
  token is printed to the console on startup so you can enter it from
  another device. Refuses to start if combined with `YTPLD_DEBUG=1`, since
  that combination would expose the debugger to your whole network.

## Getting cookies (for private playlists, age-restricted videos, or "Sign in to confirm you're not a bot" errors)

YouTube sometimes blocks or restricts requests that look like they're coming
from a script rather than a logged-in browser. Exporting your browser's
YouTube cookies and handing them to yt-dlp fixes this in most cases. Using
an Incognito/Private window keeps this export clean and separate from your
regular browsing session/extensions.

1. **Install the extension** — search your browser's extension store for
   **"Get cookies.txt LOCALLY"** and add it. (Avoid similarly-named older
   extensions that export in a different, incompatible format.)

2. **Allow it to run in Incognito**:
   - Chrome: go to `chrome://extensions`, find "Get cookies.txt LOCALLY",
     click **Details**, and toggle **Allow in Incognito**.
   - Firefox: Private Browsing windows allow extensions by default if the
     extension requests it — check `about:addons` → the extension's
     **Details** → **Run in Private Windows** if it's not already active.

3. **Open an Incognito/Private window** and go to **youtube.com**. Log in
   with the account tied to the playlist/videos you need (only necessary
   for private or restricted content — for the "not a bot" error, any
   logged-in account works).

4. **Export the cookies**:
   - Click the extension's icon in the toolbar.
   - Make sure you're on a youtube.com tab so it captures the right domain.
   - Click **Export** (or **Current Site**) to download a `cookies.txt` file.

5. **Close the Incognito window** once you're done — this ends that
   session cleanly without leaving it logged in.

6. **Save `cookies.txt` next to `app.py`.**

   ⚠️ `app.py` doesn't currently read this file — nothing will change until
   `cookiefile` support is added to the yt-dlp options in the code. Let me
   know if you'd like that wired in.

**Keep `cookies.txt` private.** It's equivalent to your login session for
whatever site it was exported from — don't share it, commit it to a repo,
or upload it anywhere. Cookies also expire, so if downloads start failing
again after a while, just re-export a fresh one.

## Troubleshooting
- **"Missing or invalid app token"** → this shouldn't happen from the app's
  own UI (it sends the token automatically). It usually means a stale
  `settings.json` from a different install, or you're calling `/api/*`
  routes directly (e.g. via curl) without the token. Check the console
  output or `settings.json` for the current token.
- **"Couldn't read that playlist"** → the link isn't public, or `yt-dlp` is
  out of date (YouTube changes things often):
  `pip install -U yt-dlp --break-system-packages`.
- **"Sign in to confirm you're not a bot" / sudden 403 errors** → see
  "Getting cookies" above.
- **Downloads fail with an ffmpeg error** → ffmpeg isn't installed or not on PATH.
- **Port 5000 already in use** (common on macOS, AirPlay uses it) → change
  `port=5000` at the bottom of `app.py`.
- **No cover art on a track** → some videos genuinely have no usable
  thumbnail image, or the file's format doesn't support embedded art in a way
  your player recognizes.
- **Missing lyrics on a track** → neither lrclib.net nor YouTube had anything
  usable for that track — try a different `subtitle_lang` in preferences for
  the YouTube-caption fallback, though not every video has captions at all.
- **Wrong or missing release year** → only set when YouTube Music exposes
  real release metadata for that track; most regular (non-Music) uploads
  don't have this, so the tag is left blank rather than guessed.

## Resource usage

Rough estimates, not measured benchmarks — actual numbers vary by OS,
Python version, and platform-specific wheels. Use the commands at the end
of this section to check your own install exactly.

**Disk (Python dependencies only, in a venv):**

| Package | Approx. size | Why |
|---|---|---|
| yt-dlp | ~20–25 MB | Bundles extractor modules for hundreds of sites in one package |
| Flask + deps (Werkzeug, Jinja2, etc.) | ~3–5 MB | Small, mostly pure Python |
| requests + deps (urllib3, certifi, etc.) | ~5–7 MB | certifi's CA bundle is a few hundred KB on its own |
| mutagen | ~1–2 MB | Pure Python |
| Pillow | ~5–15 MB | Bundles compiled image-codec libraries |
| **Total** | **~35–55 MB** | Before counting Python itself |

Not part of the pip install, but needed separately:
- **ffmpeg** — commonly 60–130 MB depending on platform/build.
- **tkinter** — free on macOS/Windows (ships with Python); a few MB via
  `python3-tk` on Linux.

**Runtime memory (RSS):**
- yt-dlp is imported eagerly at startup (it registers every extractor at
  import time), adding roughly 15–25 MB on top of Flask's own ~25–40 MB —
  so an idle server sits around **~50–65 MB**.
- Pillow and mutagen are imported lazily, only inside the functions that
  use them, specifically so a browse/search/preview-only session never
  pays their cost. The first real download (which embeds cover art) pulls
  Pillow in and pushes usage toward **~70–90 MB**, staying there for the
  rest of the process's life since Python caches imports.
- ffmpeg runs as its own subprocess, so its memory isn't part of the Flask
  process — audio extraction is light (tens of MB); the optional video
  compression pass is heavier (encoding buffers frames, potentially a few
  hundred MB for HD/4K), freed once that process exits.
- Concurrency multiplies ffmpeg's cost more than Flask's: several
  simultaneous audio conversions stay light, but several simultaneous
  video compressions will load every CPU core and use meaningfully more
  memory than the Python process itself.

**Check your own numbers:**
```bash
# Installed package sizes
du -sh venv/lib/python*/site-packages/* | sort -h

# Running process RSS
ps -o rss,command -p $(pgrep -f "python3 app.py")
```



**Not supported at all:**
- **Spotify** — deliberately excluded, not a bug. Spotify's streams are
  DRM-protected; yt-dlp can read Spotify metadata but cannot download the
  actual audio, so there's nothing legitimate this app could offer for it.
- **Live streams / ongoing broadcasts** — yt-dlp is built for on-demand
  video, not capturing an in-progress stream. Attempting one will likely
  fail outright or produce only a partial, unreliable capture.
- **DRM-protected content generally** (paid rentals, some premium-tier
  streaming embeds, etc.) — same reasoning as Spotify: if the platform
  encrypts the stream, no yt-dlp-based tool can download it.

**Listed as supported, but unreliable without extra steps:**
Several sites carry a working badge in the UI but yt-dlp's extractors for
them are known to be inconsistent without a logged-in session, even for
public content — the app surfaces a warning when your pasted link matches
one of these, but it's worth knowing upfront:
- **TikTok** — frequently blocks non-browser requests outright, public
  video or not.
- **Instagram** and **Facebook** — usually require a logged-in session for
  essentially anything, public posts included.
- **Vimeo** — public links work fine; private/unlisted links need the
  access code kept in the URL after the video ID, or they won't resolve.
- **Twitch** — subscriber-only VODs need a login; public VODs/clips are fine.
- **Bilibili** — some content is region-locked to mainland China and will
  fail regardless of login.
- **Dailymotion** — age-restricted videos need a login; most other content
  works fine.
- None of these currently support the cookie-based fix described in
  "Getting cookies" above — that section's instructions are YouTube-specific
  for now (`cookiefile` isn't wired into the yt-dlp options yet for any
  site, YouTube included — see the note in that section).

**General limitations, not specific to any one site:**
- **yt-dlp lags site changes** — YouTube (and others) change their internals
  often enough that extraction can break until yt-dlp ships a fix, sometimes
  a few days' gap. `pip install -U yt-dlp` is the fix once one's out.
- **Large playlists slow down duplicate detection** — the within-playlist
  near-duplicate check compares each title against every previously-seen
  title in that playlist. Fine up to a few hundred tracks; a playlist in
  the thousands could make Compare noticeably slower.
- **Video compression is slow and CPU-bound** — unlike downloading itself
  (network-bound), the optional light/strong compression pass re-encodes
  the whole file with ffmpeg. A long video can take a while, and running
  several compressions at once (high concurrency + video mode) will load
  every CPU core, not just the network.
- **Higher concurrency risks rate-limiting** — the concurrency setting goes
  up to 6, but pushing it that high on a large batch increases the odds of
  a temporary block from whichever site you're downloading from. 2–3 is a
  safer default for anything but small batches.
- **The app token isn't encrypted at rest** — it sits in plaintext in
  `settings.json`. Fine as long as that file stays untouched and ungitted
  (see `.gitignore`), but it's not a vault.
- **LAN mode sends the token over plain HTTP** — `MIMICRY_ALLOW_LAN=1` has
  no TLS in front of it, so anyone able to sniff traffic on that network
  could capture the token from a request. Only enable this on networks you
  actually trust, as the README already warns above.
- **The folder picker needs tkinter** — on Linux installs without it, the
  picker button errors out; typing the download path manually always works
  as a fallback regardless.
