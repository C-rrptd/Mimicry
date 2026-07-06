"""
YouTube Playlist Downloader — simple local web app
-----------------------------------------------------
Paste a playlist link, see thumbnails + titles, pick what to grab,
choose a format and download folder, and download with cover art +
tags embedded.

Run:
    pip install flask yt-dlp requests mutagen Pillow --break-system-packages
    (ffmpeg must be installed and on PATH)
    python3 app.py
Then open http://127.0.0.1:5000
"""

import os
import io
import re
import json
import glob
import time
import uuid
import secrets
import subprocess
import threading
import difflib
import html
from pathlib import Path
from collections import OrderedDict

import yt_dlp
import requests
from flask import Flask, request, jsonify, render_template, send_file, Response, stream_with_context

# Pillow and mutagen are imported lazily, inside the specific functions that
# use them (read_ytid_tag, write_ytid_tag, fix_year_tag, embed_cover_art,
# embed_lyrics, cropped_youtube_thumbnail) rather than here at module level.
# Measured in isolation, Pillow alone adds ~16MB of resident memory the
# moment it's imported; mutagen is smaller but not free either. Importing
# both eagerly means every server start pays that cost immediately, even for
# a session that only ever browses/previews and never actually downloads
# anything. Python caches imports in sys.modules, so the first function that
# needs them pays the cost once and every subsequent call (in that or any
# other function) is a near-free dict lookup — this only changes *when* the
# memory gets allocated, not how much total work importing them takes.

app = Flask(__name__)

# Settings file lives next to app.py, independent of whatever folder the user
# picks for downloads, so it survives a folder change.
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"
SETTINGS_LOCK = threading.Lock()

# Path.home() / "Music" resolves to each user's own folder (e.g.
# C:\Users\<name>\Music on Windows, ~/Music on Mac/Linux) — unlike a fixed
# path like C:\Music, this needs no special permissions (writing to a
# drive's root often requires admin rights) and works correctly if this
# tool is shared with other people or run on a shared machine.
DEFAULT_DOWNLOAD_DIR = Path.home() / "Music" / "YT Playlist Downloads"

# in-memory job store: {job_id: {"items": {id: {"status", "pct"}}, "done": bool}}
JOBS = {}
JOBS_LOCK = threading.Lock()

# Reused across preview requests so repeated clicks (skipping between
# tracks) get connection pooling / keep-alive to googlevideo.com instead of
# a fresh TCP+TLS handshake every single time.
PREVIEW_SESSION = requests.Session()

# A lyrics lookup does up to two round trips (/get, then /search as a
# fallback) to the same host, and a download batch can trigger this once
# per track — a plain requests.get() opens a fresh TCP+TLS connection every
# single time. Reusing one Session gives keep-alive connection reuse across
# all of that, same reasoning as PREVIEW_SESSION above.
LRCLIB_SESSION = requests.Session()

# yt-dlp's extract_info() call — needed to resolve a real, directly-
# streamable googlevideo.com URL for a video ID — is the slow part of a
# preview request (typically 1-3 seconds). That cost is unavoidable on the
# *first* preview of a given track, but the resolved URL stays valid for
# hours, so caching it here makes every repeat click on that same track
# (replaying, scrubbing after the player closed, misclicks) come back
# instantly instead of re-resolving from scratch each time.
PREVIEW_URL_CACHE = {}  # video id -> {"url", "ext", "headers", "expires"}
PREVIEW_URL_CACHE_LOCK = threading.Lock()
PREVIEW_URL_CACHE_TTL = 4 * 60 * 60  # seconds; conservative vs. googlevideo's actual ~6h validity


def _prune_preview_url_cache(now: float):
    """Expired entries were previously only ever dropped one-at-a-time on a
    403/404 retry — anything previewed once and never replayed just sat
    here past its expiry forever. Called on every write (see
    resolve_preview_stream) so a long session with many distinct previewed
    tracks doesn't grow this unboundedly."""
    stale = [vid for vid, entry in PREVIEW_URL_CACHE.items() if entry["expires"] <= now]
    for vid in stale:
        del PREVIEW_URL_CACHE[vid]

# Finished jobs just sit in memory forever otherwise — harmless for a quick
# session, but a slow leak on a server left running for weeks. Anything
# that's been done longer than this gets swept out the next time a new job
# is created; polling a pruned job_id just 404s, same as any unknown id.
JOB_TTL_SECONDS = 30 * 60


def prune_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items() if j.get("done") and j.get("done_at", 0) < cutoff]
        for jid in stale:
            del JOBS[jid]

# Audio format presets. "best" keeps the original stream's codec with no
# re-encoding at all — since YouTube's source audio is already lossy, this is
# the only option that adds zero additional quality loss. flac/wav give you a
# lossless *container*, but re-encode from a lossy source, so they don't
# recover quality that isn't there — they just avoid stacking a second lossy
# encode on top (which mp3 does). Worth knowing before picking one for
# "better quality": the ceiling is set by YouTube's source stream either way.
AUDIO_FORMAT_PRESETS = {
    "best": {"preferredcodec": "best"},                       # no re-encode, truest to source
    "flac": {"preferredcodec": "flac"},                        # lossless container, re-encoded
    "wav":  {"preferredcodec": "wav"},                         # uncompressed, re-encoded
    "mp3":  {"preferredcodec": "mp3", "preferredquality": "0"},# smallest, another lossy pass
    "m4a":  {"preferredcodec": "m4a", "preferredquality": "0"},# AAC in an m4a container
    # Fixed at 128kbps regardless of the global MP3_QUALITY preference — a
    # guaranteed small-file option in the main dropdown, no separate settings
    # trip required. Roughly a third the size of a "0"/V0 mp3, still fine
    # for casual listening, noticeably worse on good headphones.
    "mp3_small": {"preferredcodec": "mp3", "preferredquality": "128"},
}

# Caps the downloaded video's height. None = uncompromising (whatever the
# highest available resolution is, which for a lot of YouTube content now
# means 4K — multiple GB for a single music video). Picking a smaller cap
# is the single biggest lever on video file size, well beyond anything
# re-encoding settings could do without hurting quality.
VIDEO_QUALITY_CAPS = {
    "best": None,
    "1080": 1080,
    "720": 720,
    "480": 480,
}

# Container format for video downloads — separate lever from quality/height:
# mp4 is the most universally compatible, mkv/webm avoid an extra re-mux
# yt-dlp would otherwise do to force mp4 when the source streams don't
# already match that container.
VIDEO_FORMAT_OPTIONS = {"mp4", "mkv", "webm"}

# This is the actual size-optimization lever for video, separate from
# picking a lower resolution: re-encoding at a higher CRF (more
# compression) shrinks the file further at a GIVEN resolution, trading
# some visual quality for size. CRF scales aren't the same number across
# codecs, hence separate values per codec family rather than one number
# applied to both. Container format alone (VIDEO_FORMAT_OPTIONS above) is
# just a remux and barely touches file size — this is what actually does.
VIDEO_COMPRESS_PRESETS = {
    "none":   None,
    "light":  {"x264_crf": 23, "vp9_crf": 33},   # small size cut, hard to notice
    "strong": {"x264_crf": 28, "vp9_crf": 38},   # clearly smaller, some visible softness
}


def compress_video(path: Path, container: str, level: str) -> bool:
    """Re-encodes a video file in place at a higher compression level than
    its original download used — the only way to shrink a video meaningfully
    at the SAME resolution (resolution caps shrink it a different way, by
    fetching a smaller source to begin with; the two are independent and
    can be combined).

    Runs ffmpeg directly (not through a yt-dlp postprocessor) so the codec/
    CRF pairing here stays explicit and testable rather than depending on
    yt-dlp's internal postprocessor-arg naming for a given version.

    Returns True if compression ran and replaced the file. False (original
    file left untouched) if the level is "none" or ffmpeg failed — a failed
    compression pass should never leave you with no file at all."""
    preset = VIDEO_COMPRESS_PRESETS.get(level)
    if not preset:
        return False

    if container == "webm":
        vcodec, crf, acodec = "libvpx-vp9", preset["vp9_crf"], "libopus"
        extra = ["-b:v", "0"]  # required for libvpx-vp9's constant-quality mode
        speed_flag = ["-speed", "2"]
    else:  # mp4 and mkv both re-encode through x264/aac here
        vcodec, crf, acodec = "libx264", preset["x264_crf"], "aac"
        extra = []
        speed_flag = ["-preset", "fast"]

    tmp_out = path.with_name(path.stem + ".compressing" + path.suffix)
    cmd = [
        "ffmpeg", "-y", "-i", str(path),
        "-c:v", vcodec, "-crf", str(crf), *extra, *speed_flag,
        "-c:a", acodec,
        str(tmp_out),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=1800)
        if result.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
            if tmp_out.exists():
                tmp_out.unlink()
            return False
        tmp_out.replace(path)
        return True
    except Exception:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass
        return False

# Extension -> mimetype, used both for serving already-downloaded files and
# for labeling live-streamed previews.
EXT_MIME = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".mkv": "video/x-matroska",
    ".opus": "audio/webm",
    ".mp4": "video/mp4",
}


# ---------- settings / download dir ----------

def _load_settings_locked() -> dict:
    """Same as load_settings(), but assumes SETTINGS_LOCK is already held.
    Only for use inside a `with SETTINGS_LOCK:` block that spans a full
    read-modify-write (see set_download_dir/set_preferences) — calling this
    without the lock held is a bug."""
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_settings_locked(settings: dict):
    """Same as save_settings(), but assumes SETTINGS_LOCK is already held."""
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def load_settings() -> dict:
    with SETTINGS_LOCK:
        return _load_settings_locked()


def save_settings(settings: dict):
    with SETTINGS_LOCK:
        _save_settings_locked(settings)


_settings = load_settings()
DOWNLOAD_DIR = Path(_settings.get("download_dir", str(DEFAULT_DOWNLOAD_DIR)))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Every /api/* route is gated behind this token (see the before_request hook
# below) — otherwise, the moment this app is ever reachable beyond
# 127.0.0.1 (LAN access, a misconfigured firewall, etc.), literally anyone
# who can reach the port could trigger downloads, delete library files, or
# pop a native folder-picker dialog on your desktop with no login of any
# kind. Generated once and persisted, same as the download folder — your
# own browser gets it automatically from the page it loads; nothing else
# does.
APP_TOKEN = _settings.get("app_token")
if not APP_TOKEN:
    APP_TOKEN = secrets.token_urlsafe(32)
    save_settings({**_settings, "app_token": APP_TOKEN})


# Two endpoints are loaded directly as a URL (<audio src="...">), not via
# fetch() — the browser has no way to attach a custom header to those
# requests, so they accept the token as a query param instead. Every other
# /api/* route goes through fetch() (see the wrapped fetch in index.html),
# which attaches it as a header.
TOKEN_VIA_QUERY_PARAM_PREFIXES = ("/api/preview/", "/api/library/file/")


@app.before_request
def _require_app_token():
    path = request.path
    if not path.startswith("/api/"):
        return None  # the page itself and static assets aren't gated
    supplied = request.headers.get("X-App-Token")
    if not supplied and path.startswith(TOKEN_VIA_QUERY_PARAM_PREFIXES):
        supplied = request.args.get("token")
    if not supplied or not secrets.compare_digest(supplied, APP_TOKEN):
        return jsonify({"ok": False, "error": "Missing or invalid app token."}), 401
    return None

# Concurrency and mp3 quality were previously hardcoded constants — both are
# now user-adjustable (persisted alongside the download folder) so they
# survive a restart without editing this file.
CONCURRENCY = max(1, min(6, int(_settings.get("concurrency", 2))))
MP3_QUALITY = str(_settings.get("mp3_quality", "0"))  # "0" = best VBR; or a bitrate like "192"

# How many extra attempts a failed track gets before it's marked "error" for
# good — most download failures are transient (a network hiccup, YouTube
# briefly rate-limiting, a file lock antivirus is holding) and succeed on a
# second try with zero user action needed. 2 retries (3 attempts total) is
# the default; 0 disables retrying entirely.
MAX_RETRIES = max(0, min(5, int(_settings.get("max_retries", 2))))

# Opt-in: bakes YouTube's captions (manual if available, else auto-generated)
# into the audio file's lyrics tag. Off by default — it costs an extra
# subtitle fetch per track, and auto-captions are often rough.
EMBED_LYRICS = bool(_settings.get("embed_lyrics", False))
SUBTITLE_LANG = str(_settings.get("subtitle_lang", "en"))


def set_download_dir(new_dir: Path):
    global DOWNLOAD_DIR
    DOWNLOAD_DIR = new_dir
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Read-modify-write held under one lock (not load then save separately)
    # so a concurrent set_preferences() call can't read a stale dict, write
    # its own change, and silently erase this one (or vice versa).
    with SETTINGS_LOCK:
        updated = _load_settings_locked()
        updated["download_dir"] = str(DOWNLOAD_DIR)
        _save_settings_locked(updated)


def set_preferences(concurrency=None, mp3_quality=None, embed_lyrics=None, subtitle_lang=None, max_retries=None):
    """Updates concurrency/mp3-quality/lyrics/retry settings in memory and
    persists them. Values are validated here rather than trusting the
    caller — this is reachable from a request body."""
    global CONCURRENCY, MP3_QUALITY, EMBED_LYRICS, SUBTITLE_LANG, MAX_RETRIES
    # Same reasoning as set_download_dir: one lock spanning the full
    # read-modify-write, so two concurrent preference updates (or a
    # preference update racing a download-dir change) can't clobber
    # each other's write to settings.json.
    with SETTINGS_LOCK:
        updated = _load_settings_locked()
        if concurrency is not None:
            CONCURRENCY = max(1, min(6, int(concurrency)))
            updated["concurrency"] = CONCURRENCY
        if mp3_quality is not None:
            mp3_quality = str(mp3_quality).strip()
            if mp3_quality not in {"0", "128", "192", "256", "320"}:
                raise ValueError(f"Unsupported mp3 quality: {mp3_quality}")
            MP3_QUALITY = mp3_quality
            updated["mp3_quality"] = MP3_QUALITY
        if embed_lyrics is not None:
            EMBED_LYRICS = bool(embed_lyrics)
            updated["embed_lyrics"] = EMBED_LYRICS
        if subtitle_lang is not None:
            subtitle_lang = str(subtitle_lang).strip() or "en"
            SUBTITLE_LANG = subtitle_lang
            updated["subtitle_lang"] = SUBTITLE_LANG
        if max_retries is not None:
            MAX_RETRIES = max(0, min(5, int(max_retries)))
            updated["max_retries"] = MAX_RETRIES
        _save_settings_locked(updated)


# ---------- ID tracking: embedded tag, not a filename or index file ----------
#
# The YouTube video ID is written into a tag inside the audio file itself
# (TXXX for mp3/wav, a freeform atom for m4a/mp4, a vorbis comment for flac)
# instead of appearing in the filename or a separate .json index. Filenames
# stay clean; "is this downloaded?" is answered by reading the tag back out.
# Files from the previous version of this app (which put "[id]" at the end
# of the filename) are still recognized as a fallback, so nothing already
# downloaded gets treated as missing after this update.

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".mkv", ".flac", ".wav", ".webm", ".opus"}
IMAGE_EXTS = {".webp", ".jpg", ".jpeg", ".png"}
ID_PATTERN = re.compile(r"\[([a-zA-Z0-9_-]{11})\]")

TXXX_DESC = "ytid"
MP4_FREEFORM_KEY = "----:com.ytpldownloader:ytid"

# Parsing a file's tags with mutagen is the dominant cost of every playlist
# comparison, preview lookup, and "what's missing" check — existing_id_file_map()
# re-reads every audio file's tag on every single call otherwise. Cache the
# result per file, keyed by (mtime, size) so a change to the file invalidates
# it automatically, without needing an explicit invalidation call anywhere.
TAG_CACHE: dict = {}
TAG_CACHE_LOCK = threading.Lock()


def cached_read_ytid_tag(path: Path) -> str | None:
    """Same as read_ytid_tag(), but skips re-parsing a file's tags if its
    mtime/size haven't changed since the last check."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    fingerprint = (st.st_mtime, st.st_size)
    with TAG_CACHE_LOCK:
        cached = TAG_CACHE.get(key)
        if cached and cached[0] == fingerprint:
            return cached[1]
    ytid = read_ytid_tag(path)
    with TAG_CACHE_LOCK:
        TAG_CACHE[key] = (fingerprint, ytid)
    return ytid


def read_ytid_tag(path: Path) -> str | None:
    """Reads the embedded YouTube video ID back out of an audio file's tags,
    if present. Formats mutagen can't tag (webm/opus) simply return None."""
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    from mutagen.flac import FLAC
    suf = path.suffix.lower()
    try:
        if suf == ".mp3" or suf == ".wav":
            if suf == ".wav":
                from mutagen.wave import WAVE
                tags = WAVE(path).tags
            else:
                tags = ID3(path)
            if not tags:
                return None
            frames = tags.getall(f"TXXX:{TXXX_DESC}")
            if frames and frames[0].text:
                return str(frames[0].text[0])
        elif suf in (".m4a", ".mp4"):
            tags = MP4(path)
            val = tags.get(MP4_FREEFORM_KEY)
            if val:
                return bytes(val[0]).decode("utf-8", "ignore")
        elif suf == ".flac":
            tags = FLAC(path)
            val = tags.get("YTID")
            if val:
                return str(val[0])
    except Exception:
        return None
    return None


def determine_release_year(info: dict) -> str | None:
    """Best-effort real release year for the song — distinct from the
    video's YouTube upload date, which is what ffmpeg's FFmpegMetadata
    postprocessor uses by default and often has nothing to do with when
    the song itself came out (a 1990 song re-uploaded in 2019 gets tagged
    "2019"). yt-dlp exposes an actual release_date/release_year for
    YouTube Music tracks when the metadata is available; that's preferred
    here over the upload date."""
    release_year = info.get("release_year")
    if release_year:
        return str(release_year)
    release_date = info.get("release_date")  # YYYYMMDD
    if release_date and len(str(release_date)) >= 4:
        return str(release_date)[:4]
    return None


def fix_year_tag(path: Path, year: str | None) -> None:
    """Overwrites the year tag ffmpeg's metadata postprocessor set from the
    video's upload date with the song's real release year when one was
    available, or clears the tag out entirely otherwise — a missing year
    is less misleading than a confidently wrong one."""
    from mutagen.id3 import ID3, TDRC, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.flac import FLAC
    suf = path.suffix.lower()
    try:
        if suf == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TDRC")
            if year:
                tags.add(TDRC(encoding=3, text=year))
            tags.save(path)
        elif suf == ".wav":
            from mutagen.wave import WAVE
            audio = WAVE(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("TDRC")
            if year:
                audio.tags.add(TDRC(encoding=3, text=year))
            audio.save()
        elif suf in (".m4a", ".mp4"):
            tags = MP4(path)
            if year:
                tags["\xa9day"] = [year]
            elif "\xa9day" in tags:
                del tags["\xa9day"]
            tags.save()
        elif suf == ".flac":
            tags = FLAC(path)
            if year:
                tags["DATE"] = [year]
            elif "DATE" in tags:
                del tags["DATE"]
            tags.save()
    except Exception:
        pass  # non-critical — worst case the upload-date year stays as-is


def write_ytid_tag(path: Path, vid: str) -> bool:
    """Embeds the YouTube video ID into the audio file's own tags. Returns
    False (silently) for containers mutagen can't write to, e.g. webm/opus
    from the 'best' (no re-encode) preset — those just won't be detected as
    already-downloaded on a future check, which is a minor, disclosed
    limitation rather than a crash."""
    from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4FreeForm
    from mutagen.flac import FLAC
    suf = path.suffix.lower()
    try:
        if suf == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.setall(f"TXXX:{TXXX_DESC}", [TXXX(encoding=3, desc=TXXX_DESC, text=[vid])])
            tags.save(path)
            return True
        elif suf == ".wav":
            from mutagen.wave import WAVE
            audio = WAVE(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.setall(f"TXXX:{TXXX_DESC}", [TXXX(encoding=3, desc=TXXX_DESC, text=[vid])])
            audio.save()
            return True
        elif suf in (".m4a", ".mp4"):
            tags = MP4(path)
            tags[MP4_FREEFORM_KEY] = [MP4FreeForm(vid.encode("utf-8"))]
            tags.save()
            return True
        elif suf == ".flac":
            tags = FLAC(path)
            tags["YTID"] = [vid]
            tags.save()
            return True
    except Exception:
        return False
    return False


def embed_cover_art(path: Path, jpeg_bytes: bytes) -> bool:
    """Embeds JPEG cover art into the audio file's own tags. Converting to
    JPEG first (done by the caller) matters: YouTube's thumbnail is webp,
    but mp3's ID3 APIC frame and m4a's 'covr' atom both require jpeg/png —
    webp gets silently rejected, which is why cover art wasn't sticking for
    those formats before."""
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.flac import FLAC, Picture
    suf = path.suffix.lower()
    try:
        if suf == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg_bytes))
            tags.save(path)
            return True
        elif suf == ".wav":
            from mutagen.wave import WAVE
            audio = WAVE(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg_bytes))
            audio.save()
            return True
        elif suf in (".m4a", ".mp4"):
            tags = MP4(path)
            tags["covr"] = [MP4Cover(jpeg_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            tags.save()
            return True
        elif suf == ".flac":
            tags = FLAC(path)
            tags.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = jpeg_bytes
            tags.add_picture(pic)
            tags.save()
            return True
    except Exception:
        return False
    return False


# ---------- cover art source: YouTube thumbnail only ----------

def guess_artist_title(info: dict) -> tuple[str, str]:
    """Best-effort artist/title split from yt-dlp's info dict, used to query
    a real album-art source. YouTube Music auto-generated 'Topic' channels
    are named '<Artist> - Topic', which is a reliable artist source when
    yt-dlp hasn't already parsed one out."""
    title = info.get("track") or info.get("title") or ""
    artist = info.get("artist") or info.get("creator") or ""
    channel = info.get("uploader") or info.get("channel") or ""

    if not artist and channel.endswith(" - Topic"):
        artist = channel[: -len(" - Topic")]

    if not artist:
        parts = re.split(r"\s+-\s+", title, maxsplit=1)
        if len(parts) == 2:
            artist, title = parts

    return artist.strip(), title.strip()


def cropped_youtube_thumbnail(thumb_path: Path) -> bytes | None:
    """Fallback cover art: YouTube's own video thumbnail, center-cropped to
    a square so it doesn't carry the 16:9 letterbox border into a music
    player's cover-art slot."""
    from PIL import Image
    try:
        with Image.open(thumb_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            side = min(w, h)
            left, top = (w - side) // 2, (h - side) // 2
            im = im.crop((left, top, left + side, top + side))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
    except Exception:
        return None


def fetch_cover_art(info: dict, thumb_path: Path | None) -> bytes | None:
    """Cover art sourced only from YouTube's own video thumbnail, center-
    cropped to a square. No external lookups (e.g. iTunes/Apple Music)."""
    if thumb_path and thumb_path.exists():
        return cropped_youtube_thumbnail(thumb_path)
    return None


# ---------- lyrics: lrclib.net database preferred, YouTube captions as fallback ----------

LRCLIB_BASE = "https://lrclib.net/api"


def fetch_lrclib_lyrics(artist: str, title: str, duration: int | None = None) -> tuple[str, str] | None:
    """Looks up lyrics from lrclib.net — a free, keyless, community-sourced
    lyrics database — in preference to YouTube's auto-generated captions.
    Auto-captions are speech-to-text tuned for spoken content: they're
    frequently wrong for singing, and often simply don't exist for music
    videos at all. A real lyrics database is a better primary source;
    YouTube captions remain a fallback for the (mostly obscure/unreleased)
    tracks lrclib doesn't have.

    Returns (lyrics_text, kind) where kind is 'synced' (LRC-format,
    timestamped — what we want) or 'plain' (no timing info, still usable),
    or None if nothing matched closely enough to trust."""
    if not title:
        return None

    # /get is an exact-match lookup — fast and precise when it hits, since
    # it also matches on duration. Try it first.
    params = {"track_name": title, "artist_name": artist or ""}
    if duration:
        params["duration"] = int(duration)
    try:
        resp = LRCLIB_SESSION.get(f"{LRCLIB_BASE}/get", params=params, timeout=8)
        if resp.status_code == 200:
            data = resp.json() or {}
            synced, plain = data.get("syncedLyrics"), data.get("plainLyrics")
            if synced:
                return synced, "synced"
            if plain:
                return plain, "plain"
    except (requests.RequestException, ValueError):
        pass

    # No exact hit (wrong duration, slightly different title, etc.) — fall
    # back to fuzzy /search and only accept a result whose title actually
    # matches ours, using the same matching logic as the compare/dedup
    # views, so a wrong song doesn't get tagged as this track's lyrics.
    try:
        resp = LRCLIB_SESSION.get(
            f"{LRCLIB_BASE}/search",
            params={"track_name": title, "artist_name": artist or ""},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json() or []
    except (requests.RequestException, ValueError):
        return None

    for candidate in results:
        if titles_match(title, candidate.get("trackName") or ""):
            synced, plain = candidate.get("syncedLyrics"), candidate.get("plainLyrics")
            if synced:
                return synced, "synced"
            if plain:
                return plain, "plain"
    return None


SRT_CUE_NUMBER_PATTERN = re.compile(r"^\d+$")
VTT_TAG_PATTERN = re.compile(r"<[^>]+>")


def subtitle_to_plain_text(path: Path) -> str:
    """Converts a downloaded .vtt/.srt subtitle file into plain lyrics text
    — strips cue numbers, timestamp lines, and inline formatting tags, and
    collapses the duplicate/rolling lines auto-generated captions produce."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    lines = []
    last_line = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("WEBVTT") or upper.startswith("NOTE") or upper.startswith("KIND:") or upper.startswith("LANGUAGE:"):
            continue
        if "-->" in line or SRT_CUE_NUMBER_PATTERN.match(line):
            continue
        line = VTT_TAG_PATTERN.sub("", line).strip()
        if not line or line == last_line:
            continue  # auto-captions often repeat the previous line while "rolling"
        lines.append(line)
        last_line = line

    return "\n".join(lines)



VTT_TIMESTAMP_PATTERN = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
)


def _lrc_timestamp(timestamp: str) -> str | None:
    """Convert a VTT/SRT timestamp to enhanced-LRC [mm:ss.xx] form."""
    match = VTT_TIMESTAMP_PATTERN.search(timestamp)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2)) + hours * 60
    seconds = int(match.group(3))
    centiseconds = int(match.group(4)) // 10
    return f"[{minutes:02d}:{seconds:02d}.{centiseconds:02d}]"


def subtitle_to_lrc(path: Path) -> str:
    """Convert downloaded VTT/SRT captions to synchronized LRC.

    Uses each cue's start timestamp, strips VTT formatting, unescapes HTML
    entities, and suppresses consecutive duplicate rolling-caption lines.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    output = []
    pending_timestamp = None
    last_text = None

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        upper = line.upper()
        if (upper.startswith("WEBVTT") or upper.startswith("NOTE")
                or upper.startswith("KIND:") or upper.startswith("LANGUAGE:")):
            continue
        if SRT_CUE_NUMBER_PATTERN.fullmatch(line):
            continue

        if "-->" in line:
            pending_timestamp = _lrc_timestamp(line.split("-->", 1)[0].strip())
            continue

        if pending_timestamp:
            lyric = html.unescape(VTT_TAG_PATTERN.sub("", line)).strip()
            lyric = re.sub(r"\s+", " ", lyric)
            if lyric and lyric != last_text:
                output.append(f"{pending_timestamp}{lyric}")
                last_text = lyric
            pending_timestamp = None

    return "\n".join(output) + ("\n" if output else "")


def write_lrc_sidecar(audio_path: Path, subtitle_path: Path) -> bool:
    """Write synchronized lyrics beside the audio using the same basename."""
    lrc = subtitle_to_lrc(subtitle_path)
    if not lrc:
        return False
    try:
        audio_path.with_suffix(".lrc").write_text(lrc, encoding="utf-8")
        return True
    except OSError:
        return False


def embed_lyrics(path: Path, lyrics: str) -> bool:
    """Embeds plain-text lyrics into the audio file's own tags — USLT for
    mp3/wav, the '©lyr' atom for m4a/mp4, a LYRICS vorbis comment for flac."""
    if not lyrics:
        return False
    from mutagen.id3 import ID3, USLT, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.flac import FLAC
    suf = path.suffix.lower()
    try:
        if suf == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
            tags.save(path)
            return True
        elif suf == ".wav":
            from mutagen.wave import WAVE
            audio = WAVE(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("USLT")
            audio.tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
            audio.save()
            return True
        elif suf in (".m4a", ".mp4"):
            tags = MP4(path)
            tags["\xa9lyr"] = [lyrics]
            tags.save()
            return True
        elif suf == ".flac":
            tags = FLAC(path)
            tags["LYRICS"] = [lyrics]
            tags.save()
            return True
    except Exception:
        return False
    return False


def find_downloaded_file(vid: str) -> Path | None:
    """Locate an already-downloaded file for a video ID — checked via the
    legacy '[id]' filename fallback first (cheap), then by reading the
    embedded tag out of each audio file."""
    if not DOWNLOAD_DIR.exists():
        return None
    files = [f for f in DOWNLOAD_DIR.iterdir() if f.is_file()]
    for f in files:
        if f"[{vid}]" in f.stem:
            return f
    for f in files:
        if f.suffix.lower() in AUDIO_EXTS and cached_read_ytid_tag(f) == vid:
            return f
    return None


def cleanup_orphan_thumbnails():
    """Removes leftover .webp/.jpg/.png thumbnail files that have no
    matching audio file with the same name next to them. These are left
    behind when a download's cover-art-embed step failed partway through —
    the song never actually finished, but a stray thumbnail file remains and
    can be mistaken for a completed download."""
    if not DOWNLOAD_DIR.exists():
        return
    files = [f for f in DOWNLOAD_DIR.iterdir() if f.is_file()]
    stems_with_audio = {f.stem for f in files if f.suffix.lower() in AUDIO_EXTS}
    for f in files:
        if f.suffix.lower() in IMAGE_EXTS and f.stem not in stems_with_audio:
            try:
                f.unlink()
            except OSError:
                pass


# ---------- dedup helpers ----------

def normalize_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"official|video|audio|lyrics?|hd|4k|remaster(ed)?", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


QUOTED_TITLE_PATTERN = re.compile(r'["\u201c\u201d\u2018\u2019\']([^"\u201c\u201d\u2018\u2019\']{2,})["\u201c\u201d\u2018\u2019\']')
LEADING_TRACK_NUMBER_PATTERN = re.compile(r"^\s*\d+\s*[\.\)\-:]\s*")


def title_candidates(title: str) -> set:
    """Every normalized form of a title worth comparing against another
    title — not just the raw whole-string normalization. Real-world video
    titles often wrap the actual song name in extra branding, e.g.
    'Burnice EP – "Burning Desires" | Zenless Zone Zero' vs. a plainly
    named 'Burning Desires' upload elsewhere. Comparing only the full
    normalized strings misses this entirely (they're mostly different
    text), so this also pulls out: text in quotes (often the actual song
    title in EP/OST-style video titles), and the title with a leading
    track number stripped ('61. Burning Desires' -> 'Burning Desires')."""
    cands = set()
    norm_full = normalize_title(title)
    if norm_full:
        cands.add(norm_full)
    for q in QUOTED_TITLE_PATTERN.findall(title or ""):
        norm_q = normalize_title(q)
        if norm_q:
            cands.add(norm_q)
    stripped = LEADING_TRACK_NUMBER_PATTERN.sub("", title or "")
    if stripped != title:
        norm_stripped = normalize_title(stripped)
        if norm_stripped:
            cands.add(norm_stripped)
    return cands


def titles_match(title_a: str, title_b: str, threshold: float = 0.9, boilerplate: frozenset = frozenset()) -> bool:
    """True if two titles likely refer to the same song. Checks every
    candidate normalization of each title (see title_candidates) against
    every candidate of the other — an exact candidate match, or a short
    candidate fully contained in a longer one (catches a plain title buried
    inside a longer branded one), count immediately.

    The fuzzy fallback only compares words that AREN'T shared 'boilerplate'
    across the current batch of titles (see boilerplate_words()). Without
    this, two completely different songs uploaded by the same channel with
    a long identical suffix — e.g. 'Devil Trigger - Cover by X feat Y,
    Episode 12' vs 'Bury the Light - Cover by X feat Y, Episode 12' — can
    score above threshold on a whole-string similarity ratio simply because
    most of the characters are identical branding, not the song name."""
    cands_a = title_candidates(title_a)
    cands_b = title_candidates(title_b)
    for a in cands_a:
        for b in cands_b:
            if a == b:
                return True
            shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
            if len(shorter) >= 4 and shorter in longer:
                return True

    words_a = set(normalize_title(title_a).split()) - boilerplate
    words_b = set(normalize_title(title_b).split()) - boilerplate
    if not words_a or not words_b:
        return False  # nothing distinctive left on one side to compare
    core_a, core_b = " ".join(sorted(words_a)), " ".join(sorted(words_b))
    return difflib.SequenceMatcher(None, core_a, core_b).ratio() >= threshold


def build_local_title_index(local_files):
    """Precomputes each local file's normalized title candidates and word
    set once, instead of recomputing them from scratch inside a nested loop.

    /api/compare previously called titles_match(title, local_stem) once per
    (playlist entry, local file) pair, and titles_match() calls
    title_candidates() — a few regex passes — fresh every single time. On a
    large library (thousands of files) compared against a large playlist,
    that's the dominant cost of the whole request: the same regex work for
    the same local file gets redone once per playlist entry. Precomputing it
    here means the regex work happens once per local file, period.

    Returns (indexed, word_index):
      indexed:     list of (local_stem, local_name, candidate_set, word_set)
      word_index:  normalized word -> list of indices into `indexed` whose
                   word_set contains it (used to narrow the fuzzy fallback
                   to files that share at least one word, instead of
                   checking literally every file in the library)."""
    indexed = []
    word_index: dict = {}
    for local_stem, local_name in local_files:
        cands = title_candidates(local_stem)
        words = set(normalize_title(local_stem).split())
        idx = len(indexed)
        indexed.append((local_stem, local_name, cands, words))
        for w in words:
            word_index.setdefault(w, []).append(idx)
    return indexed, word_index


def find_local_title_match(title: str, indexed: list, word_index: dict,
                            boilerplate: frozenset = frozenset(), threshold: float = 0.9):
    """Same matching semantics as looping titles_match(title, local_stem)
    over every entry in `indexed` in order and returning the first hit, but
    using the precomputed candidates/word-sets from build_local_title_index()
    instead of recomputing them per comparison.

    The exact/substring pass still checks every local file (cheap set/string
    ops, no regex) so its result matches titles_match() exactly. The fuzzy
    fallback narrows to files sharing at least one non-boilerplate word
    before running difflib — in the extremely unlikely case two titles share
    zero words but would still clear the 0.9 character-similarity threshold,
    this fallback won't find them, which titles_match() technically would.
    That tradeoff is what makes the fallback sub-linear in library size."""
    cands_a = title_candidates(title)

    for local_stem, local_name, cands_b, _words_b in indexed:
        for a in cands_a:
            for b in cands_b:
                if a == b:
                    return local_name
                shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                if len(shorter) >= 4 and shorter in longer:
                    return local_name

    words_a = set(normalize_title(title).split()) - boilerplate
    if not words_a:
        return None
    candidate_indices = set()
    for w in words_a:
        candidate_indices.update(word_index.get(w, ()))
    core_a = " ".join(sorted(words_a))
    for idx in sorted(candidate_indices):
        _local_stem, local_name, _cands_b, words_b_full = indexed[idx]
        words_b = words_b_full - boilerplate
        if not words_b:
            continue
        core_b = " ".join(sorted(words_b))
        if difflib.SequenceMatcher(None, core_a, core_b).ratio() >= threshold:
            return local_name
    return None


def boilerplate_words(titles, min_ratio: float = 0.4, min_count: int = 3) -> frozenset:
    """Words that show up across a large fraction of the given titles —
    channel-wide branding ('cover', 'episode', a recurring series name)
    rather than anything specific to one song. Needs at least a handful of
    titles to tell 'common across this batch' apart from coincidence, so
    returns nothing for small batches."""
    word_sets = [set(normalize_title(t).split()) for t in titles]
    n = len(word_sets)
    if n < 3:
        return frozenset()
    from collections import Counter
    df = Counter()
    for ws in word_sets:
        for w in ws:
            df[w] += 1
    cutoff = max(min_count, int(n * min_ratio))
    return frozenset(w for w, c in df.items() if c >= cutoff)


def existing_id_file_map() -> dict:
    """Video ID -> matching local filename, for every track already
    downloaded — found via the legacy '[id]' filename fallback plus the
    embedded tag in each audio file. Also sweeps out orphaned thumbnail-only
    leftovers first, so a failed partial download doesn't get mistaken for
    a completed one. This is the single source of truth used both for the
    quick "is this downloaded" checks and for the full playlist-vs-disk
    comparison view."""
    cleanup_orphan_thumbnails()
    mapping = {}
    if not DOWNLOAD_DIR.exists():
        return mapping
    files = [f for f in DOWNLOAD_DIR.iterdir() if f.is_file()]
    current_paths = {str(f) for f in files}
    # TAG_CACHE only gets an entry explicitly removed when a file is deleted
    # through this app's own library "Delete" button — a file removed any
    # other way (manually in Explorer/Finder, moved, or just because the
    # download folder got pointed somewhere else) leaves a permanently
    # orphaned entry behind otherwise. This does a full directory scan
    # anyway, so reconciling against the real file list here is free.
    with TAG_CACHE_LOCK:
        stale_keys = [key for key in TAG_CACHE if key not in current_paths]
        for key in stale_keys:
            del TAG_CACHE[key]
    for f in files:
        m = ID_PATTERN.search(f.stem)
        if m:
            mapping[m.group(1)] = f.name
            continue
        if f.suffix.lower() in AUDIO_EXTS:
            tagged = cached_read_ytid_tag(f)
            if tagged:
                mapping[tagged] = f.name
    return mapping


def existing_ids() -> set:
    """Video IDs already downloaded. See existing_id_file_map()."""
    return set(existing_id_file_map().keys())


def flag_duplicates(entries):
    """Mark entries with near-identical titles as possible dupes."""
    boilerplate = boilerplate_words([e["title"] for e in entries])
    seen = []
    for i, e in enumerate(entries):
        e["is_dupe"] = False
        for prev_title, prev_i in seen:
            if titles_match(e["title"], prev_title, boilerplate=boilerplate):
                e["is_dupe"] = True
                e["dupe_of"] = entries[prev_i]["title"]
                break
        seen.append((e["title"], i))
    return entries


# ---------- playlist metadata cache ----------
#
# Fetch, Compare, "Download missing", and "Check missing" all start by
# re-reading the playlist from YouTube via yt-dlp's flat extraction, even
# when they're run back-to-back on the same URL (a very common sequence:
# fetch, then immediately Compare). That's a full network round-trip each
# time for data that hasn't changed in the last few seconds. Cache the
# result briefly per URL — short enough that a playlist you just edited on
# YouTube shows up as changed within a minute, long enough to make a quick
# fetch -> compare -> download-missing sequence feel instant.

# ---------- multi-site support ----------
#
# yt-dlp already has extractors for well over a thousand sites, and
# fetch_playlist_info()/download_one_track() work on any of them with no
# special-casing at all — see the comments on those functions. The only
# thing that's actually YouTube/SoundCloud/Twitter/Reddit-specific here is
# this mapping, and it's purely cosmetic: it decides what badge a track
# shows in the UI. A site missing from this dict still downloads perfectly
# fine — it just shows a generic "web" badge instead of its real name.
# Adding a site here is *only* ever a labeling improvement, never something
# that unlocks new download capability.
#
# Spotify is deliberately not included: Spotify's own streams are DRM-
# protected and yt-dlp cannot download them (only fetch metadata), so
# there's nothing this app can legitimately offer for it.
EXTRACTOR_TO_SITE = {
    "youtube": "youtube",
    "youtubemusic": "youtube",
    "youtube:music": "youtube",
    "youtube:search": "youtube",
    "youtube:tab": "youtube",
    "soundcloud": "soundcloud",
    "soundcloud:set": "soundcloud",
    "soundcloud:playlist": "soundcloud",
    "twitter": "twitter",
    "twitter:card": "twitter",
    "reddit": "reddit",
    "vimeo": "vimeo",
    "vimeo:album": "vimeo",
    "vimeo:showcase": "vimeo",
    "twitch:vod": "twitch",
    "twitch:clips": "twitch",
    "twitch:stream": "twitch",
    "tiktok": "tiktok",
    "tiktok:user": "tiktok",
    "bandcamp": "bandcamp",
    "bandcamp:album": "bandcamp",
    "bandcamp:weekly": "bandcamp",
    "instagram": "instagram",
    "instagram:story": "instagram",
    "facebook": "facebook",
    "dailymotion": "dailymotion",
    "bilibili": "bilibili",
}


def is_audio_only_source(url: str) -> bool:
    """YouTube Music, SoundCloud, and Bandcamp never have a real video
    stream worth previewing — YT Music watch pages are audio-first (often
    no video track at all, or just a static cover-art placeholder), and
    SoundCloud/Bandcamp are audio-only by nature. Video preview is forced
    back to audio for these regardless of what's requested."""
    u = (url or "").lower()
    return "music.youtube.com" in u or "soundcloud.com" in u or "bandcamp.com" in u


def classify_site(extractor_key: str, url: str) -> str:
    """Best-effort mapping from yt-dlp's extractor name (or, failing that,
    the URL's domain) to a short site key, purely for the UI badge —
    downloading itself doesn't depend on getting this right, since the real
    URL is always what's used. YouTube Music is treated as plain YouTube —
    same catalog and extractor, just a different front end."""
    key = (extractor_key or "").lower()
    if key in EXTRACTOR_TO_SITE:
        return EXTRACTOR_TO_SITE[key]
    u = (url or "").lower()
    if "music.youtube.com" in u or "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "soundcloud.com" in u:
        return "soundcloud"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "reddit.com" in u:
        return "reddit"
    if "vimeo.com" in u:
        return "vimeo"
    if "twitch.tv" in u:
        return "twitch"
    if "tiktok.com" in u:
        return "tiktok"
    if "bandcamp.com" in u:
        return "bandcamp"
    if "instagram.com" in u:
        return "instagram"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    if "dailymotion.com" in u:
        return "dailymotion"
    if "bilibili.com" in u:
        return "bilibili"
    return "other"


ENTRY_URL_CACHE_MAX = 5000
ENTRY_URL_CACHE: "OrderedDict[str, str]" = OrderedDict()
ENTRY_URL_CACHE_LOCK = threading.Lock()


def remember_entry_url(vid: str | None, url: str | None) -> None:
    if not vid or not url:
        return
    with ENTRY_URL_CACHE_LOCK:
        ENTRY_URL_CACHE[vid] = url
        ENTRY_URL_CACHE.move_to_end(vid)
        while len(ENTRY_URL_CACHE) > ENTRY_URL_CACHE_MAX:
            ENTRY_URL_CACHE.popitem(last=False)


def resolve_entry_url(vid: str, fallback_url: str | None = None) -> str:
    """Turns an id back into a real, fetchable URL. Prefers whatever URL
    that id was actually listed/searched under; falls back to a URL the
    caller supplies (e.g. one the browser sent back alongside the id); and
    only as a last resort assumes it's a bare 11-char YouTube id, which
    keeps old queue items (from before this cache existed) working."""
    with ENTRY_URL_CACHE_LOCK:
        cached = ENTRY_URL_CACHE.get(vid)
    if cached:
        return cached
    if fallback_url:
        remember_entry_url(vid, fallback_url)
        return fallback_url
    return f"https://www.youtube.com/watch?v={vid}"


PLAYLIST_CACHE_TTL_SECONDS = 60
_playlist_cache: dict = {}
_playlist_cache_lock = threading.Lock()

# Every distinct URL ever fetched gets its own entry above, and — unlike
# JOBS, which has prune_old_jobs() — nothing was ever removing an entry once
# its TTL passed, only overwriting it if that same URL was fetched again.
# Left running for a long time across many different playlist/video/search
# URLs, that's an unbounded, slow memory leak. Prune expired entries here
# (called on every write, so it self-cleans without needing a background
# thread) rather than just checking staleness at read time.
def _prune_playlist_cache(now: float):
    stale = [u for u, (ts, _info) in _playlist_cache.items() if (now - ts) >= PLAYLIST_CACHE_TTL_SECONDS]
    for u in stale:
        del _playlist_cache[u]


def fetch_playlist_info(url: str) -> dict:
    """Flat-extracts playlist/video info for a URL via yt-dlp, reusing a
    recent result for the same URL instead of hitting YouTube again.
    Raises whatever yt-dlp raises on failure — same as calling extract_info
    directly, just with a cache layer in front."""
    now = time.time()
    with _playlist_cache_lock:
        cached = _playlist_cache.get(url)
        if cached and (now - cached[0]) < PLAYLIST_CACHE_TTL_SECONDS:
            return cached[1]

    opts = {"extract_flat": True, "quiet": True, "skip_download": True}
    with yt_dlp.YoutubeDL({**opts, "socket_timeout": 15}) as ydl:
        info = ydl.extract_info(url, download=False)

    with _playlist_cache_lock:
        _prune_playlist_cache(now)
        _playlist_cache[url] = (now, info)
    return info


# ---------- routes ----------

@app.route("/")
def index():
    return render_template("index.html", download_dir=str(DOWNLOAD_DIR), app_token=APP_TOKEN)


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify({
        "ok": True,
        "download_dir": str(DOWNLOAD_DIR),
        "concurrency": CONCURRENCY,
        "mp3_quality": MP3_QUALITY,
        "embed_lyrics": EMBED_LYRICS,
        "subtitle_lang": SUBTITLE_LANG,
        "max_retries": MAX_RETRIES,
    })


@app.route("/api/preferences", methods=["POST"])
def api_set_preferences():
    data = request.get_json(force=True)
    try:
        set_preferences(
            concurrency=data.get("concurrency"),
            mp3_quality=data.get("mp3_quality"),
            embed_lyrics=data.get("embed_lyrics"),
            subtitle_lang=data.get("subtitle_lang"),
            max_retries=data.get("max_retries"),
        )
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Invalid preference: {e}"}), 400
    return jsonify({
        "ok": True,
        "concurrency": CONCURRENCY,
        "mp3_quality": MP3_QUALITY,
        "embed_lyrics": EMBED_LYRICS,
        "subtitle_lang": SUBTITLE_LANG,
        "max_retries": MAX_RETRIES,
    })


@app.route("/api/set-download-dir", methods=["POST"])
def api_set_download_dir():
    data = request.get_json(force=True)
    path_str = (data.get("path") or "").strip()
    if not path_str:
        return jsonify({"ok": False, "error": "Path is empty."}), 400

    p = Path(path_str)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Can't create/access that folder: {e}"}), 400

    if not p.is_dir():
        return jsonify({"ok": False, "error": "That path isn't a folder."}), 400

    set_download_dir(p)
    return jsonify({"ok": True, "download_dir": str(DOWNLOAD_DIR)})


FOLDER_PICKER_SCRIPT = """
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
chosen = filedialog.askdirectory(title="Choose download folder", initialdir=sys.argv[1])
root.destroy()
print(chosen)
"""


@app.route("/api/pick-folder", methods=["POST"])
def api_pick_folder():
    """Opens a native OS folder-picker dialog. Since this app only ever runs
    on your own machine (bound to 127.0.0.1), it's safe for a browser request
    to trigger a native dialog on your desktop — there's no remote user who
    could be doing this to you.

    Run as a short-lived subprocess rather than calling Tkinter in-process:
    Flask's threaded server handles this request on a worker thread, but
    Tkinter (especially the Cocoa backend on macOS) expects GUI calls on a
    process's *main* thread — calling tk.Tk() from a non-main thread can
    hang or crash the whole app rather than just this request. A subprocess
    gets its own main thread for free, so the dialog is safe regardless of
    which worker thread handled the request, and a crash in the dialog
    process can't take the server down with it."""
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-c", FOLDER_PICKER_SCRIPT, str(DOWNLOAD_DIR)],
            capture_output=True,
            text=True,
            timeout=300,  # generous — this is a human picking a folder, not a fast call
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Folder picker timed out."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't open the folder picker: {e}"}), 500

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
            return jsonify({
                "ok": False,
                "error": "tkinter isn't available in this Python install — type the folder path manually instead."
            }), 500
        return jsonify({"ok": False, "error": f"Couldn't open the folder picker: {stderr or 'unknown error'}"}), 500

    chosen = (result.stdout or "").strip()
    if not chosen:
        return jsonify({"ok": False, "error": "No folder selected."})

    return jsonify({"ok": True, "path": chosen})


# ---------- library browser: search/play/delete what's already downloaded ----------

def read_library_metadata(path: Path) -> tuple[str, str, float | None]:
    """Title, artist, and duration for the library view, in one pass. yt-dlp's
    FFmpegMetadata postprocessor already writes title/artist during download,
    so these are normally populated — falls back to the filename stem for
    older files or formats where that write didn't happen.

    Previously this was two separate functions, each independently calling
    mutagen.File() on the same path — parsing every track's container twice
    for no reason. One open now serves both title/artist (via easy=True,
    which still exposes .info alongside the simplified tag names) and
    duration (from .info.length)."""
    try:
        import mutagen
        audio = mutagen.File(path, easy=True)
        title = artist = None
        if audio and audio.tags:
            title = (audio.tags.get("title") or [None])[0]
            artist = (audio.tags.get("artist") or [None])[0]
        duration = round(audio.info.length, 1) if audio and audio.info else None
        return (title or path.stem), (artist or ""), duration
    except Exception:
        return path.stem, "", None


def resolve_library_file(filename: str) -> Path | None:
    """Safely resolves a filename to a real audio file directly inside
    DOWNLOAD_DIR — refuses anything that would escape it (e.g. '../x') or
    that points into a subfolder, since downloads are always flat."""
    if not filename:
        return None
    try:
        candidate = (DOWNLOAD_DIR / filename).resolve()
        download_root = DOWNLOAD_DIR.resolve()
    except OSError:
        return None
    if candidate.parent != download_root:
        return None
    if not candidate.is_file() or candidate.suffix.lower() not in AUDIO_EXTS:
        return None
    return candidate


@app.route("/api/library", methods=["GET"])
def api_library():
    """Everything currently downloaded, independent of any specific
    playlist — backs the library browser's search/play/delete view.

    Also flags possible duplicates *within the folder itself* by title —
    something /api/compare can't catch, since it only ever looks at files
    tied to the one playlist you're comparing against. Two downloads of the
    same song from different playlists (or different channels' uploads)
    never show up together there; this is the one place that checks
    everything on disk against everything else on disk."""
    query = (request.args.get("q") or "").strip().lower()
    if not DOWNLOAD_DIR.exists():
        return jsonify({"ok": True, "entries": [], "total": 0, "dupe_count": 0})

    all_files = sorted(
        (f for f in DOWNLOAD_DIR.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS),
        key=lambda p: p.name.lower(),
    )

    full_entries = []
    for f in all_files:
        title, artist, duration = read_library_metadata(f)
        try:
            size_bytes = f.stat().st_size
        except OSError:
            size_bytes = 0
        full_entries.append({
            "filename": f.name,
            "title": title,
            "artist": artist,
            "duration": duration,
            "format": f.suffix.lower().lstrip("."),
            "size_bytes": size_bytes,
        })

    # Run over the WHOLE library, not just whatever matches the current
    # search box — filtering to "bury" shouldn't hide that this file is
    # also a duplicate of something named completely differently.
    boilerplate = boilerplate_words([e["title"] for e in full_entries])
    seen = []
    for e in full_entries:
        e["is_dupe"] = False
        e["dupe_of"] = None
        for prev_title, prev_filename in seen:
            if titles_match(e["title"], prev_title, boilerplate=boilerplate):
                e["is_dupe"] = True
                e["dupe_of"] = {"filename": prev_filename, "title": prev_title}
                break
        seen.append((e["title"], e["filename"]))

    dupe_count = len([e for e in full_entries if e["is_dupe"]])

    if query:
        entries = [
            e for e in full_entries
            if query in e["title"].lower() or query in e["artist"].lower() or query in e["filename"].lower()
        ]
    else:
        entries = full_entries

    # A dupe note is only worth a clickable jump-to link if the file it
    # points at is actually rendered in this (possibly filtered) result set
    # — otherwise it'd be a dead link to a row that doesn't exist right now.
    visible_filenames = {e["filename"] for e in entries}
    for e in entries:
        if e["dupe_of"]:
            e["dupe_of"]["visible"] = e["dupe_of"]["filename"] in visible_filenames

    return jsonify({"ok": True, "entries": entries, "total": len(entries), "dupe_count": dupe_count})


@app.route("/api/library/file/<path:filename>")
def api_library_file(filename):
    path = resolve_library_file(filename)
    if not path:
        return jsonify({"ok": False, "error": "File not found."}), 404
    mime = EXT_MIME.get(path.suffix.lower(), "application/octet-stream")
    return send_file(path, mimetype=mime, conditional=True)


@app.route("/api/library/delete", methods=["POST"])
def api_library_delete():
    data = request.get_json(force=True)
    filenames = data.get("filenames", [])
    if not filenames:
        return jsonify({"ok": False, "error": "No files specified."}), 400

    deleted, failed = [], []
    for name in filenames:
        path = resolve_library_file(name)
        if not path:
            failed.append(name)
            continue
        try:
            path.unlink()
            deleted.append(name)
            with TAG_CACHE_LOCK:
                TAG_CACHE.pop(str(path), None)
        except OSError:
            failed.append(name)

    return jsonify({"ok": True, "deleted": deleted, "failed": failed})


# ---------- M3U export ----------

def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name or "").strip()
    return name[:150] or "playlist"


@app.route("/api/export-m3u", methods=["POST"])
def api_export_m3u():
    """Writes an .m3u file into the download folder, listing this
    playlist's already-downloaded tracks in their original order. Tracks
    that aren't downloaded yet are skipped rather than pointing at a file
    that doesn't exist."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Paste a playlist URL first."}), 400

    try:
        info = fetch_playlist_info(url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't read that playlist: {e}"}), 400

    raw_entries = (info or {}).get("entries", [])
    if not raw_entries:
        return jsonify({"ok": False, "error": "No videos found — check the link is a valid playlist."}), 400

    file_map = existing_id_file_map()
    playlist_title = (info or {}).get("title") or "Playlist"

    lines = ["#EXTM3U"]
    matched = 0
    for e in raw_entries:
        if not e:
            continue
        filename = file_map.get(e.get("id"))
        if not filename:
            continue
        duration = int(e.get("duration") or 0)
        artist = guess_display_artist(e)
        title = e.get("title") or "(untitled)"
        label = f"{artist} - {title}" if artist else title
        lines.append(f"#EXTINF:{duration},{label}")
        lines.append(filename)  # relative — the .m3u is written into the same folder
        matched += 1

    if matched == 0:
        return jsonify({"ok": False, "error": "None of this playlist's tracks have been downloaded yet."}), 400

    m3u_name = safe_filename(playlist_title) + ".m3u"
    (DOWNLOAD_DIR / m3u_name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    return jsonify({
        "ok": True,
        "filename": m3u_name,
        "matched_count": matched,
        "total": len([e for e in raw_entries if e]),
    })


def guess_display_artist(e: dict) -> str:
    """Artist name for the track list — same heuristic used for cover-art
    lookups (strip '- Topic' from auto-generated channels, or split
    'Artist - Title' style video titles), falling back to the raw
    uploader/channel name if neither heuristic finds anything."""
    artist, _ = guess_artist_title(e)
    return artist or e.get("uploader") or e.get("channel") or ""


def build_entries(raw_entries):
    """Shared formatting for anything that produces a list of video entries —
    playlist listing and search results both funnel through this so their
    output shape (and dedup/already-downloaded flagging) stays identical."""
    have = existing_ids()
    entries = []
    for e in raw_entries:
        if not e:
            continue
        # Cast to str: YouTube ids are always alphanumeric strings, but
        # several other extractors (Twitter, Reddit, TikTok, ...) hand back
        # a plain int for "id". Left as an int, it round-trips through JSON
        # as a bare number — which then silently breaks any strict (===/!==)
        # comparison against a value read from an HTML data-* attribute
        # (always a string), since 123 !== "123". That's exactly what was
        # making the per-track "remove from queue" button do nothing for
        # entries from those sites.
        vid = e.get("id")
        vid = str(vid) if vid is not None else vid
        source_url = e.get("webpage_url") or e.get("url") or ""
        site = classify_site(e.get("ie_key") or e.get("extractor_key") or e.get("extractor"), source_url)
        remember_entry_url(vid, source_url)
        entries.append({
            "id": vid,
            "url": source_url,
            "site": site,
            "audio_only": is_audio_only_source(source_url),
            "title": e.get("title") or "(untitled)",
            "artist": guess_display_artist(e),
            "duration": e.get("duration"),
            "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else e.get("thumbnail"),
            "already_downloaded": vid in have,
        })
    return flag_duplicates(entries)


@app.route("/api/list", methods=["POST"])
def api_list():
    url = (request.get_json(force=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Paste a playlist URL first."}), 400

    try:
        info = fetch_playlist_info(url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't read that link: {e}"}), 400

    # A playlist URL gives back an 'entries' list. A single video URL
    # doesn't — yt-dlp just returns that one video's own info dict directly.
    # Wrapping it as a one-item list lets it flow through the exact same
    # build_entries()/dedup/already-downloaded pipeline as a playlist would,
    # so a lone video shows up in the queue exactly like any other track.
    if info and "entries" in info:
        raw_entries = info.get("entries") or []
        title = info.get("title", "Playlist")
    elif info:
        raw_entries = [info]
        title = info.get("title", "Video")
    else:
        raw_entries = []
        title = "Playlist"

    if not raw_entries:
        return jsonify({"ok": False, "error": "No videos found — check the link is a valid video or playlist."}), 400

    return jsonify({
        "ok": True,
        "playlist_title": title,
        "entries": build_entries(raw_entries),
    })


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(force=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Type something to search for."}), 400

    # Browse/search is YouTube-only — it's the only one of the supported
    # sites with a "search by keyword" extractor. SoundCloud/Twitter/Reddit
    # require pasting a direct link instead (the "Fetch" box handles that).
    try:
        opts = {"extract_flat": True, "quiet": True, "skip_download": True}
        with yt_dlp.YoutubeDL({**opts, "socket_timeout": 15}) as ydl:
            # ytsearch15: pulls the top 15 YouTube results for the query —
            # same flat extraction yt-dlp uses for playlists, just backed by
            # YouTube's search endpoint instead of a playlist ID.
            info = ydl.extract_info(f"ytsearch15:{query}", download=False)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Search failed: {e}"}), 400

    raw_entries = (info or {}).get("entries", [])
    if not raw_entries:
        return jsonify({"ok": False, "error": "No results found."}), 400

    return jsonify({
        "ok": True,
        "playlist_title": f'Search results for "{query}"',
        "entries": build_entries(raw_entries),
    })


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    mode = data.get("mode", "audio")  # "audio" or "video"
    audio_format = data.get("audio_format", "best")  # "best" | "flac" | "wav" | "mp3" | "mp3_small" | "m4a"
    video_quality = data.get("video_quality", "best")  # "best" | "1080" | "720" | "480"
    video_format = data.get("video_format", "mp4")  # "mp4" | "mkv" | "webm"
    video_compress = data.get("video_compress", "none")  # "none" | "light" | "strong"
    playlist_url = data.get("url", "")
    # Optional {id: url} map sent alongside ids — the browser already has
    # this from whatever /api/list or /api/search response added the track,
    # so sending it back here means a track still downloads from its real
    # site even if the server process restarted and lost ENTRY_URL_CACHE.
    client_urls = data.get("urls") or {}

    if not ids:
        return jsonify({"ok": False, "error": "No tracks selected."}), 400
    if mode == "audio" and audio_format not in AUDIO_FORMAT_PRESETS:
        return jsonify({"ok": False, "error": f"Unknown format: {audio_format}"}), 400
    if mode == "video" and video_quality not in VIDEO_QUALITY_CAPS:
        return jsonify({"ok": False, "error": f"Unknown video quality: {video_quality}"}), 400
    if mode == "video" and video_format not in VIDEO_FORMAT_OPTIONS:
        return jsonify({"ok": False, "error": f"Unknown video format: {video_format}"}), 400
    if mode == "video" and video_compress not in VIDEO_COMPRESS_PRESETS:
        return jsonify({"ok": False, "error": f"Unknown compression level: {video_compress}"}), 400

    url_map = {vid: resolve_entry_url(vid, client_urls.get(vid)) for vid in ids}

    prune_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "items": {vid: {"status": "queued", "pct": 0, "title": "", "lyrics": None} for vid in ids},
            "done": False,
        }

    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, ids, url_map, mode, audio_format, video_quality, video_format, video_compress),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Unknown job."}), 404
        return jsonify({"ok": True, **job})


def download_one_track(job_id, vid, video_url, mode, audio_format, outtmpl, video_quality="best", video_format="mp4", video_compress="none"):
    """Downloads and tags a single track. Runs inside the worker pool in
    run_download_job — each call is independent (own yt-dlp instance, own
    progress-hook closure), so several can run at once without stepping on
    each other.

    video_url is the track's real source URL (YouTube, SoundCloud,
    Twitter/X, or Reddit) — resolved by the caller via resolve_entry_url(),
    not assumed to be YouTube."""

    # SoundCloud/YT Music/Bandcamp have no video stream at all — requesting
    # "video" mode for one of these would just fail with yt-dlp's "Requested
    # format is not available" error. Silently downloading the audio instead
    # (rather than erroring) matches what the preview button already does
    # for these sources — see is_audio_only_source().
    if mode == "video" and is_audio_only_source(video_url):
        mode = "audio"
        if audio_format not in AUDIO_FORMAT_PRESETS:
            audio_format = "best"

    # The id is embedded in the *download* filename (stripped back out via
    # a rename below once everything's finished) rather than just using
    # the plain "%(title)s.%(ext)s" template passed in — two entries with
    # the same title (a remix, a re-upload, the same song pulled from two
    # different sites — not rare in a big playlist) would otherwise
    # download to the literal same path. With CONCURRENCY > 1 (the
    # default), those two downloads can be in flight at the same time,
    # racing to write/convert/tag the same file — worst case, one silently
    # clobbers the other and the job reports both "done" despite only one
    # file existing. Since the id is always unique, this guarantees two
    # tracks can never collide regardless of what they're titled or how
    # many run in parallel.
    outtmpl = str(DOWNLOAD_DIR / f"%(title)s [{vid}].%(ext)s")

    def hook(d, vid=vid):
        with JOBS_LOCK:
            item = JOBS[job_id]["items"][vid]
            if d["status"] == "downloading":
                item["status"] = "downloading"
                pct = d.get("_percent_str", "0%").strip()
                try:
                    item["pct"] = float(pct.replace("%", ""))
                except ValueError:
                    pass
                item["title"] = d.get("info_dict", {}).get("title", item.get("title", ""))
            elif d["status"] == "finished":
                item["status"] = "processing"
                item["pct"] = 100

    opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
        "quiet": True,
        "noprogress": True,
        "ignoreerrors": True,
        # Thumbnail is downloaded here just so we can embed it ourselves
        # below via Pillow + mutagen — more reliable than ffmpeg's own
        # embed postprocessor, which silently fails on webp source
        # images for mp3/m4a. We delete this raw file ourselves after.
        "writethumbnail": True,
    }

    if mode == "audio":
        preset = dict(AUDIO_FORMAT_PRESETS[audio_format])
        if audio_format == "mp3":
            preset["preferredquality"] = MP3_QUALITY
        extract_pp = {"key": "FFmpegExtractAudio", **preset}
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [extract_pp, {"key": "FFmpegMetadata"}],
            "postprocessor_args": {"ffmpeg": ["-avoid_negative_ts", "make_zero"]},
        })
        if EMBED_LYRICS:
            opts.update({
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [SUBTITLE_LANG],
                "subtitlesformat": "vtt",
            })
    else:
        height_cap = VIDEO_QUALITY_CAPS.get(video_quality)
        if height_cap:
            format_selector = f"bestvideo[height<={height_cap}]+bestaudio/best[height<={height_cap}]"
        else:
            format_selector = "bestvideo+bestaudio/best"
        opts.update({
            "format": format_selector,
            "merge_output_format": video_format,
            "postprocessors": [
                {"key": "EmbedThumbnail"},
                {"key": "FFmpegMetadata"},
            ],
        })

    # Snapshotted before the download starts so the leftover-artifact sweep
    # below can tell "a file this exact download just created" apart from
    # "a file that was already sitting here" — e.g. an mp3 you kept on
    # purpose from an earlier flac download of the same title. Only files
    # that are new since this snapshot are eligible to be swept away.
    try:
        pre_existing_files = set(DOWNLOAD_DIR.iterdir())
    except OSError:
        pre_existing_files = set()

    total_attempts = MAX_RETRIES + 1
    last_error = None

    for attempt in range(1, total_attempts + 1):
        try:
            with JOBS_LOCK:
                item = JOBS[job_id]["items"][vid]
                item["status"] = "downloading"
                item["pct"] = 0
                item["attempt"] = attempt
                item["max_attempts"] = total_attempts
            with yt_dlp.YoutubeDL({**opts, "socket_timeout": 15}) as ydl:
                info = ydl.extract_info(video_url, download=True)
                if info is None:
                    # ignoreerrors=True swallows the real exception here rather
                    # than raising it, so this has to be checked explicitly or
                    # a failed download gets marked "done" anyway.
                    raise RuntimeError("yt-dlp returned no info (download likely failed)")

                base = Path(ydl.prepare_filename(info))
                if mode == "audio" and audio_format == "best":
                    # yt-dlp's "best" (no re-encode) extraction names the
                    # output after the source *codec*, not the pre-extraction
                    # container extension in info["ext"] — e.g. opus-in-webm
                    # audio (very common on YouTube) becomes "<name>.opus",
                    # not "<name>.webm". Guessing the extension from
                    # info["ext"] is wrong here, so instead look for whatever
                    # audio file with this base name actually got created.
                    final_path = next(
                        (p for p in base.parent.glob(glob.escape(base.stem) + ".*")
                         if p.suffix.lower() in AUDIO_EXTS),
                        None,
                    )
                elif mode == "audio":
                    final_ext = {
                        "flac": "flac",
                        "wav": "wav",
                        "mp3": "mp3",
                        "mp3_small": "mp3",  # same container as "mp3", just a lower fixed bitrate
                        "m4a": "m4a",
                    }[audio_format]
                    final_path = base.with_suffix("." + final_ext)
                else:
                    final_path = base.with_suffix("." + video_format)

                if not final_path or not final_path.exists():
                    # ignoreerrors=True means a failed extraction step can
                    # leave yt-dlp "successful" with only a stray thumbnail
                    # on disk — catch that explicitly instead of marking the
                    # track done.
                    raise RuntimeError(
                        "Audio file wasn't created (extraction likely failed) — "
                        "check ffmpeg is installed and on PATH."
                    )

                if mode == "audio":
                    # yt-dlp's postprocessor normally deletes the pre-
                    # conversion source file (e.g. the raw .opus/.webm
                    # stream) once it's been converted — but that cleanup
                    # can fail to run (a file lock from antivirus/indexing
                    # on Windows, an interrupted job, etc.). Sweep away only
                    # files that are BOTH new since pre_existing_files AND
                    # share this track's base filename — the "new" check is
                    # what keeps this from ever deleting a file you already
                    # had, e.g. an existing "Song.mp3" while downloading
                    # "Song" again as flac; only a stray leftover this exact
                    # run just created is eligible.
                    for stray in base.parent.glob(glob.escape(base.stem) + ".*"):
                        if stray == final_path or stray in pre_existing_files:
                            continue
                        if stray.suffix.lower() in AUDIO_EXTS:
                            try:
                                stray.unlink()
                            except OSError:
                                pass

                    thumb_path = next(
                        (base.with_suffix(ext) for ext in (".webp", ".jpg", ".jpeg", ".png")
                         if base.with_suffix(ext).exists()),
                        None,
                    )
                    try:
                        cover_bytes = fetch_cover_art(info, thumb_path)
                        if cover_bytes:
                            embed_cover_art(final_path, cover_bytes)
                    finally:
                        if thumb_path:
                            try:
                                thumb_path.unlink()
                            except OSError:
                                pass

                    fix_year_tag(final_path, determine_release_year(info))

                    if EMBED_LYRICS:
                        # Tracked explicitly rather than left silent: with no
                        # visible status, "no lyrics existed anywhere" and "the
                        # code never ran" look identical from the UI.
                        lyrics_text = None
                        lyrics_source = None

                        artist, guessed_title = guess_artist_title(info)
                        db_result = fetch_lrclib_lyrics(artist, guessed_title or info.get("title", ""), info.get("duration"))
                        if db_result:
                            lyrics_text, _kind = db_result
                            lyrics_source = "db"

                        sub_candidates = list(base.parent.glob(glob.escape(base.stem) + ".*.vtt")) + \
                                          list(base.parent.glob(glob.escape(base.stem) + ".*.srt"))
                        if not lyrics_text and sub_candidates:
                            # Fallback only — lrclib found nothing for this
                            # track, so fall back to YouTube's own captions
                            # (real ones if the video has them, otherwise
                            # auto-generated speech-to-text).
                            subtitle_path = sub_candidates[0]
                            synced_lyrics = subtitle_to_lrc(subtitle_path)
                            if synced_lyrics:
                                lyrics_text = synced_lyrics
                                lyrics_source = "youtube"
                        for sub_file in sub_candidates:
                            try:
                                sub_file.unlink()
                            except OSError:
                                pass

                        if lyrics_text:
                            embedded_ok = embed_lyrics(final_path, lyrics_text)
                            lyrics_status = (
                                ("embedded_db" if lyrics_source == "db" else "embedded_youtube")
                                if embedded_ok else "unsupported_format"
                            )
                        else:
                            lyrics_status = "no_lyrics_found"
                        with JOBS_LOCK:
                            JOBS[job_id]["items"][vid]["lyrics"] = lyrics_status
                    else:
                        with JOBS_LOCK:
                            JOBS[job_id]["items"][vid]["lyrics"] = "off"
                else:
                    with JOBS_LOCK:
                        JOBS[job_id]["items"][vid]["lyrics"] = "n/a"
                    if video_compress != "none":
                        compress_video(final_path, video_format, video_compress)

                # Strip the "[id]" disambiguator back out now that the
                # download/convert/tag steps are safely done — the id in
                # the filename was only ever there to make each track's
                # in-flight path unique (see the outtmpl comment above).
                # If something else already grabbed the clean name in the
                # meantime (a same-titled sibling in this batch that
                # finished first, or a pre-existing library file), the
                # id-suffixed name is kept rather than overwriting it —
                # still fully correct, just slightly less tidy.
                id_suffix = f" [{vid}]"
                if final_path.stem.endswith(id_suffix):
                    clean_path = final_path.with_name(final_path.stem[: -len(id_suffix)] + final_path.suffix)
                    if not clean_path.exists():
                        try:
                            final_path.rename(clean_path)
                            final_path = clean_path
                        except OSError:
                            pass  # keep the id-suffixed name — no data lost, just less pretty

                write_ytid_tag(final_path, vid)

            with JOBS_LOCK:
                JOBS[job_id]["items"][vid]["status"] = "done"
                JOBS[job_id]["items"][vid]["pct"] = 100
            return  # success — don't touch the remaining attempts
        except Exception as e:
            last_error = e
            if attempt < total_attempts:
                # Most failures here are transient (a network blip, YouTube
                # briefly rate-limiting, a file lock antivirus/indexing is
                # holding on Windows) — a short, increasing backoff gives
                # that a chance to clear before hammering it again
                # immediately, without stalling a real, persistent failure
                # for too long.
                with JOBS_LOCK:
                    JOBS[job_id]["items"][vid]["status"] = "retrying"
                    JOBS[job_id]["items"][vid]["error"] = str(e)
                time.sleep(min(2 * attempt, 6))
                continue

    with JOBS_LOCK:
        JOBS[job_id]["items"][vid]["status"] = "error"
        JOBS[job_id]["items"][vid]["error"] = str(last_error)


# Concurrency is now configurable via CONCURRENCY (set_preferences), not a
# fixed constant — see its definition near DOWNLOAD_DIR setup above.


def run_download_job(job_id, ids, url_map, mode, audio_format="best", video_quality="best", video_format="mp4", video_compress="none"):
    outtmpl = str(DOWNLOAD_DIR / "%(title)s.%(ext)s")

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [
            pool.submit(
                download_one_track, job_id, vid, url_map.get(vid) or resolve_entry_url(vid),
                mode, audio_format, outtmpl, video_quality, video_format, video_compress,
            )
            for vid in ids
        ]
        for f in futures:
            f.result()  # propagate nothing (errors are already caught per-track), just wait for completion

    with JOBS_LOCK:
        JOBS[job_id]["done"] = True
        JOBS[job_id]["done_at"] = time.time()


def resolve_preview_stream(vid: str, want_video: bool = False) -> dict | None:
    """Resolves (and caches) the direct media URL for a track ID, from
    whichever site it actually came from (see ENTRY_URL_CACHE / resolve_entry_url).
    See PREVIEW_URL_CACHE comment above for why caching matters here.

    want_video asks for a video+audio preview instead of audio-only — but
    is silently downgraded back to audio for YouTube Music / SoundCloud
    (see is_audio_only_source), since there's no real video there to show."""
    source_url = resolve_entry_url(vid)
    effective_want_video = want_video and not is_audio_only_source(source_url)
    # Audio and video previews of the same track resolve to different
    # stream URLs, so they need separate cache slots — but only when video
    # was actually honored; an audio-only source falls back to the plain
    # (shared) cache key rather than wasting a second entry that's
    # identical to the audio one anyway.
    cache_key = f"{vid}:video" if effective_want_video else vid

    now = time.time()
    with PREVIEW_URL_CACHE_LOCK:
        cached = PREVIEW_URL_CACHE.get(cache_key)
        if cached and cached["expires"] > now:
            return cached

    opts = {
        "quiet": True,
        "noprogress": True,
        # Video preview uses a single pre-muxed stream (audio+video already
        # combined) since there's no ffmpeg merge step here the way a real
        # download gets — that caps it below a real download's max quality
        # (YouTube's highest resolutions are video-only DASH streams that
        # require merging with a separate audio track), which is the right
        # tradeoff for an instant, no-processing preview.
        "format": "best" if effective_want_video else "bestaudio/best",
        "skip_download": True,
        # The "android" client skips the webpage scrape + JS signature
        # deciphering the default "web" client needs, which is most of
        # where the 1-3s delay comes from. Falls back to "web" for the
        # rare video android can't resolve, so this only ever helps.
        # Ignored (harmlessly) by non-YouTube extractors.
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    with yt_dlp.YoutubeDL({**opts, "socket_timeout": 15}) as ydl:
        try:
            info = ydl.extract_info(source_url, download=False)
        except Exception:
            return None

    stream_url = (info or {}).get("url")
    if not stream_url:
        return None

    resolved = {
        "url": stream_url,
        "ext": (info or {}).get("ext", "m4a"),
        "headers": dict((info or {}).get("http_headers") or {}),
        "expires": now + PREVIEW_URL_CACHE_TTL,
    }
    with PREVIEW_URL_CACHE_LOCK:
        _prune_preview_url_cache(now)
        PREVIEW_URL_CACHE[cache_key] = resolved
    return resolved


def open_preview_upstream(vid: str, range_header: str | None, want_video: bool = False, _retried: bool = False):
    """Resolves a stream URL (cached or fresh) and opens the upstream
    request. Returns (upstream_response, ext, error_message, status_code)
    — error_message is None on success. On a 403/404 (a cached URL that
    went stale earlier than our TTL estimate), the cache entry is dropped
    and resolution is retried exactly once from scratch."""
    resolved = resolve_preview_stream(vid, want_video=want_video)
    if not resolved:
        return None, None, "Couldn't resolve a stream URL for this track.", 500

    upstream_headers = dict(resolved["headers"])
    if range_header:
        upstream_headers["Range"] = range_header

    cache_key = f"{vid}:video" if (want_video and not is_audio_only_source(resolve_entry_url(vid))) else vid

    try:
        upstream = PREVIEW_SESSION.get(resolved["url"], headers=upstream_headers, stream=True, timeout=15)
    except requests.RequestException as e:
        with PREVIEW_URL_CACHE_LOCK:
            PREVIEW_URL_CACHE.pop(cache_key, None)
        return None, None, f"Couldn't reach the source site: {e}", 502

    if upstream.status_code in (403, 404):
        if not _retried:
            upstream.close()
            with PREVIEW_URL_CACHE_LOCK:
                PREVIEW_URL_CACHE.pop(cache_key, None)
            return open_preview_upstream(vid, range_header, want_video=want_video, _retried=True)
        # Still failing after a fresh URL — this isn't a stale-cache issue,
        # the source itself is refusing the request (common on TikTok/
        # Instagram/etc. without auth, or a since-deleted post). Report it
        # as an error instead of streaming the site's error page back to
        # the browser labeled as audio/video — that's what silently turns
        # into a generic "no supported source" playback failure client-side.
        upstream.close()
        return None, None, (
            f"The source site refused this request (HTTP {upstream.status_code}) — "
            "it may require login, or the post may no longer be available."
        ), 502

    return upstream, resolved["ext"], None, upstream.status_code


@app.route("/api/preview/<vid>")
def api_preview(vid):
    """
    Serve a preview for a track — the full song, not a trimmed clip.

    Priority order:
    1. If this track has already been fully downloaded, serve that real file
       directly — no network call, no re-encoding.
    2. Otherwise, proxy-stream straight from the source site on the fly,
       resolving (and caching, see PREVIEW_URL_CACHE) the direct media URL
       first. Nothing is written to disk for this — no cache folder, no
       temp files. The actual bytes are streamed through this Flask
       request and forwarded to the browser as they arrive.

    Pass ?type=video for a video+audio preview instead of audio-only —
    automatically downgraded back to audio for YouTube Music/SoundCloud
    tracks regardless (see is_audio_only_source), since there's nothing
    real to show there anyway.
    """
    existing = find_downloaded_file(vid)
    if existing:
        mime = EXT_MIME.get(existing.suffix.lower(), "application/octet-stream")
        return send_file(existing, mimetype=mime, conditional=True)

    want_video = request.args.get("type") == "video"
    range_header = request.headers.get("Range")
    upstream, ext, error, status_code = open_preview_upstream(vid, range_header, want_video=want_video)
    if error:
        return jsonify({"ok": False, "error": error}), status_code

    mime = EXT_MIME.get(f".{ext}", "application/octet-stream")

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp_headers = {"Accept-Ranges": "bytes", "Content-Type": mime}
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=resp_headers,
    )


@app.route("/api/check-missing", methods=["POST"])
def api_check_missing():
    """Re-scans a playlist and reports which tracks aren't downloaded yet,
    without downloading anything. Also cleans up orphaned thumbnail-only
    leftovers from failed downloads so those correctly count as missing."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Paste a playlist URL first."}), 400

    try:
        info = fetch_playlist_info(url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't read that playlist: {e}"}), 400

    raw_entries = (info or {}).get("entries", [])
    have = existing_ids()  # also runs orphan cleanup
    missing = [
        {"id": e.get("id"), "title": e.get("title") or "(untitled)"}
        for e in raw_entries if e and e.get("id") not in have
    ]

    return jsonify({
        "ok": True,
        "playlist_title": (info or {}).get("title", "Playlist"),
        "total": len([e for e in raw_entries if e]),
        "missing_count": len(missing),
        "missing": missing,
    })


@app.route("/api/check-status", methods=["POST"])
def api_check_status():
    """Refresh: re-checks whichever tracks are currently listed in the UI
    (playlist results, search results, or a mix) against what's actually on
    disk right now — no playlist re-fetch needed, since the ids are already
    known client-side. Used to update already-downloaded flags/checkboxes
    after downloads finish or files change outside the app."""
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "No track ids given."}), 400

    have = existing_ids()
    downloaded = {vid: (vid in have) for vid in ids}
    return jsonify({"ok": True, "downloaded": downloaded})


@app.route("/api/download-missing", methods=["POST"])
def api_download_missing():
    """Combined 'check and download undownloaded songs' action: re-scans the
    playlist, cleans up orphaned partial-download leftovers, and kicks off a
    normal download job for whatever isn't already on disk. Progress can be
    polled the same way as /api/download, via the returned job_id."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    mode = data.get("mode", "audio")
    audio_format = data.get("audio_format", "mp3")
    video_quality = data.get("video_quality", "best")
    video_format = data.get("video_format", "mp4")
    video_compress = data.get("video_compress", "none")

    if not url:
        return jsonify({"ok": False, "error": "Paste a playlist URL first."}), 400
    if mode == "audio" and audio_format not in AUDIO_FORMAT_PRESETS:
        return jsonify({"ok": False, "error": f"Unknown format: {audio_format}"}), 400
    if mode == "video" and video_quality not in VIDEO_QUALITY_CAPS:
        return jsonify({"ok": False, "error": f"Unknown video quality: {video_quality}"}), 400
    if mode == "video" and video_format not in VIDEO_FORMAT_OPTIONS:
        return jsonify({"ok": False, "error": f"Unknown video format: {video_format}"}), 400
    if mode == "video" and video_compress not in VIDEO_COMPRESS_PRESETS:
        return jsonify({"ok": False, "error": f"Unknown compression level: {video_compress}"}), 400

    try:
        info = fetch_playlist_info(url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't read that playlist: {e}"}), 400

    raw_entries = (info or {}).get("entries", [])
    raw_entries = [e for e in raw_entries if e]
    for e in raw_entries:
        if e.get("id") is not None:
            e["id"] = str(e["id"])
    have = existing_ids()  # also runs orphan cleanup
    missing_entries = [e for e in raw_entries if e.get("id") not in have]
    missing_ids = [e.get("id") for e in missing_entries]

    # Remember each missing entry's real URL (SoundCloud set, Twitter
    # thread, Reddit multi-video post, etc.) so the download step below
    # doesn't fall back to assuming it's YouTube.
    url_map = {}
    for e in missing_entries:
        vid = e.get("id")
        source_url = e.get("webpage_url") or e.get("url") or ""
        remember_entry_url(vid, source_url)
        url_map[vid] = resolve_entry_url(vid, source_url)

    if not missing_ids:
        return jsonify({
            "ok": True,
            "job_id": None,
            "missing_count": 0,
            "message": "Nothing missing — everything in this playlist is already downloaded.",
        })

    prune_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "items": {vid: {"status": "queued", "pct": 0, "title": ""} for vid in missing_ids},
            "done": False,
        }
    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, missing_ids, url_map, mode, audio_format, video_quality, video_format, video_compress),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job_id, "missing_count": len(missing_ids)})


@app.route("/api/compare", methods=["POST"])
def api_compare():
    """Full side-by-side comparison: every playlist entry alongside whether
    a matching local file was found for it. Unlike /api/check-missing (which
    only returns the missing ones and a count), this returns the whole
    playlist with each entry's status — so when a count like '79 of 80'
    shows up, you can actually see which specific track that is.

    Also separates two different reasons a count can look "off by one":
    an actual missing download, vs. the playlist itself containing the same
    track twice (same video ID repeated, or two entries with near-identical
    titles) — which only ever needs one file on disk, not two."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Paste a playlist URL first."}), 400

    try:
        info = fetch_playlist_info(url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't read that playlist: {e}"}), 400

    raw_entries = (info or {}).get("entries", [])
    if not raw_entries:
        return jsonify({"ok": False, "error": "No videos found — check the link is a valid playlist."}), 400

    file_map = existing_id_file_map()

    # Every audio file physically on disk, by filename stem — used as a
    # fallback match for entries whose video ID isn't tagged/tracked. This
    # catches the case where a song was originally downloaded from one
    # channel's upload, and a newer playlist links a *different* channel's
    # upload of the exact same song (different video ID, same title) — that
    # should read as "already have it," not "missing."
    local_files_by_title = []
    if DOWNLOAD_DIR.exists():
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                local_files_by_title.append((f.stem, f.name))
    # Precomputed once per request, not once per (playlist entry, local
    # file) pair — see build_local_title_index()/find_local_title_match().
    local_title_indexed, local_title_word_index = build_local_title_index(local_files_by_title)

    entries = []
    seen_ids = set()
    seen_titles = []  # (title, index_into_entries) for near-dupe detection
    id_first_index = {}  # video id -> index_into_entries of its first occurrence
    boilerplate = boilerplate_words([e.get("title") or "" for e in raw_entries if e])
    for i, e in enumerate(raw_entries, start=1):
        if not e:
            continue
        vid = e.get("id")
        vid = str(vid) if vid is not None else vid
        title = e.get("title") or "(untitled)"
        downloaded = vid in file_map

        is_id_dupe = vid in seen_ids
        dupe_of = None
        if is_id_dupe:
            orig = entries[id_first_index[vid]]
            dupe_of = {"index": orig["index"], "title": orig["title"], "matched_file": orig["matched_file"]}
        else:
            for prev_title, prev_idx in seen_titles:
                if titles_match(title, prev_title, boilerplate=boilerplate):
                    orig = entries[prev_idx]
                    dupe_of = {"index": orig["index"], "title": orig["title"], "matched_file": orig["matched_file"]}
                    break

        # Only worth checking against local files if this entry isn't
        # already confirmed downloaded by its own video ID — otherwise
        # it's a real match, not a fallback one.
        local_match = None
        if not downloaded:
            local_match = find_local_title_match(
                title, local_title_indexed, local_title_word_index, boilerplate=boilerplate
            )

        entries.append({
            "index": i,
            "id": vid,
            "title": title,
            "artist": guess_display_artist(e),
            "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else e.get("thumbnail"),
            "downloaded": downloaded,
            "matched_file": file_map.get(vid),
            "is_exact_dupe": is_id_dupe,
            "dupe_of": dupe_of,
            "local_title_match": local_match,
        })
        if vid not in id_first_index:
            id_first_index[vid] = len(entries) - 1
        seen_ids.add(vid)
        seen_titles.append((title, len(entries) - 1))

    # A "real" missing download excludes: exact repeats of a video ID
    # already counted elsewhere in the playlist (only ever need one file),
    # and entries that match an existing local file by title even though
    # the video ID itself was never downloaded (different channel's upload
    # of the same song).
    missing = [
        en for en in entries
        if not en["downloaded"] and not en["is_exact_dupe"] and not en["local_title_match"]
    ]
    exact_dupes = [en for en in entries if en["is_exact_dupe"]]
    near_dupes = [en for en in entries if en["dupe_of"] and not en["is_exact_dupe"]]
    title_matched = [
        en for en in entries
        if en["local_title_match"] and not en["downloaded"] and not en["is_exact_dupe"]
    ]

    folder_file_count = len([
        f for f in DOWNLOAD_DIR.iterdir()
        if DOWNLOAD_DIR.exists() and f.is_file() and f.suffix.lower() in AUDIO_EXTS
    ]) if DOWNLOAD_DIR.exists() else 0

    unique_track_count = len(entries) - len(exact_dupes)

    return jsonify({
        "ok": True,
        "playlist_title": (info or {}).get("title", "Playlist"),
        "total": len(entries),
        "unique_track_count": unique_track_count,
        "downloaded_count": len([en for en in entries if en["downloaded"]]),
        "missing_count": len(missing),
        "exact_dupe_count": len(exact_dupes),
        "title_matched_count": len(title_matched),
        "near_dupe_count": len(near_dupes),
        "folder_file_count": folder_file_count,
        "entries": entries,
    })


COMPARE_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Playlist vs. Downloaded — Comparison</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 24px; background: #0f1115; color: #e6e8ee;
         font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; }
  h1 { font-size: 20px; margin: 0 0 16px; }
  .bar { display: flex; gap: 8px; margin-bottom: 16px; }
  input[type=text] { flex: 1; padding: 10px 12px; border-radius: 8px; border: 1px solid #2a2d36;
         background: #171922; color: #e6e8ee; font-size: 14px; }
  button { padding: 10px 16px; border-radius: 8px; border: none; background: #6c7bf7;
         color: white; font-weight: 600; cursor: pointer; font-size: 14px; }
  button:disabled { opacity: 0.5; cursor: default; }
  .summary { margin-bottom: 14px; font-size: 14px; color: #b8bcc8; }
  .summary b { color: #e6e8ee; }
  .summary .missing-count { color: #ff6b6b; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; padding: 8px 10px; color: #9aa0ae; font-weight: 600;
       border-bottom: 1px solid #2a2d36; position: sticky; top: 0; background: #0f1115; }
  td { padding: 8px 10px; border-bottom: 1px solid #1c1f28; vertical-align: middle; }
  tr.missing { background: rgba(255, 107, 107, 0.08); }
  tr.missing td.title { color: #ff9a9a; }
  .thumb { width: 44px; height: 44px; object-fit: cover; border-radius: 6px; background: #1c1f28; }
  .status { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }
  .status.ok { background: rgba(60, 200, 120, 0.15); color: #4fd18a; }
  .status.missing { background: rgba(255, 107, 107, 0.15); color: #ff6b6b; }
  .file { color: #7d8497; font-size: 12px; }
  .error { color: #ff6b6b; margin-top: 10px; }
  .idx { color: #6a6f7d; width: 32px; }
</style>
</head>
<body>
  <h1>Playlist vs. Downloaded — Comparison</h1>
  <div class="bar">
    <input id="url" type="text" placeholder="Paste playlist URL...">
    <button id="go">Compare</button>
  </div>
  <div id="summary" class="summary"></div>
  <div id="error" class="error"></div>
  <table id="results" style="display:none">
    <thead>
      <tr><th class="idx">#</th><th></th><th>Title</th><th>Artist</th><th>Status</th><th>Local file</th></tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

<script>
const goBtn = document.getElementById('go');
const urlInput = document.getElementById('url');
const summary = document.getElementById('summary');
const errorEl = document.getElementById('error');
const table = document.getElementById('results');
const rows = document.getElementById('rows');

async function compare() {
  const url = urlInput.value.trim();
  if (!url) return;
  goBtn.disabled = true;
  goBtn.textContent = 'Comparing...';
  errorEl.textContent = '';
  summary.textContent = '';
  table.style.display = 'none';
  rows.innerHTML = '';

  try {
    const res = await fetch('/api/compare', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-App-Token': '__APP_TOKEN__'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (!data.ok) {
      errorEl.textContent = data.error || 'Something went wrong.';
      return;
    }

    summary.innerHTML = `<b>${data.playlist_title}</b> — ${data.downloaded_count} of ${data.total} downloaded` +
      (data.missing_count > 0 ? `, <span class="missing-count">${data.missing_count} missing</span>` : ' — all present');

    for (const e of data.entries) {
      const tr = document.createElement('tr');
      if (!e.downloaded) tr.className = 'missing';
      tr.innerHTML = `
        <td class="idx">${e.index}</td>
        <td>${e.thumbnail ? `<img class="thumb" src="${e.thumbnail}">` : ''}</td>
        <td class="title">${escapeHtml(e.title)}</td>
        <td>${escapeHtml(e.artist || '')}</td>
        <td>${e.downloaded ? '<span class="status ok">Downloaded</span>' : '<span class="status missing">Missing</span>'}</td>
        <td class="file">${e.matched_file ? escapeHtml(e.matched_file) : '—'}</td>
      `;
      rows.appendChild(tr);
    }
    table.style.display = '';
  } catch (err) {
    errorEl.textContent = 'Request failed: ' + err;
  } finally {
    goBtn.disabled = false;
    goBtn.textContent = 'Compare';
  }
}

goBtn.addEventListener('click', compare);
urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') compare(); });

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
</script>
</body>
</html>"""


@app.route("/compare")
def compare_page():
    """Standalone comparison page — playlist entries side by side with
    what's actually on disk, with the missing one(s) highlighted. Doesn't
    depend on templates/index.html, so it works regardless of what's in
    the main UI."""
    return Response(COMPARE_PAGE_HTML.replace("__APP_TOKEN__", APP_TOKEN), mimetype="text/html")


if __name__ == "__main__":
    # debug=True enables the Werkzeug debugger, which allows arbitrary code
    # execution from the browser if this ever became reachable beyond
    # localhost. Off by default; set YTPLD_DEBUG=1 while developing if you
    # want the debugger/auto-reloader back.
    debug_mode = os.environ.get("YTPLD_DEBUG") == "1"

    # 127.0.0.1 by default — nothing outside this machine can reach it.
    # Binding to 0.0.0.0 (e.g. for phone/LAN access) is a real change in who
    # can reach every endpoint, so it has to be requested explicitly rather
    # than something a copy-pasted run command could silently flip on.
    allow_lan = os.environ.get("MIMICRY_ALLOW_LAN") == "1"
    host = "0.0.0.0" if allow_lan else "127.0.0.1"

    if debug_mode and allow_lan:
        raise SystemExit(
            "Refusing to start: YTPLD_DEBUG=1 together with MIMICRY_ALLOW_LAN=1 would "
            "expose Werkzeug's debugger (arbitrary code execution) to your whole network. "
            "Use only one of these at a time."
        )

    if allow_lan:
        print("=" * 70)
        print("MIMICRY_ALLOW_LAN=1 — binding to 0.0.0.0: reachable from other")
        print("devices on your network, not just this machine.")
        print(f"App token (needed by any device to use it): {APP_TOKEN}")
        print("Anyone with your local IP AND this token can trigger downloads,")
        print("browse/delete your library, and change your download folder.")
        print("Only do this on a network you trust (e.g. your home Wi-Fi).")
        print("=" * 70)

    app.run(debug=debug_mode, host=host, port=5000, threaded=True)
