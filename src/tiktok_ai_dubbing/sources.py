"""Short-video sources through yt-dlp: TikTok, Instagram Reels, YouTube Shorts, X, Facebook and more.

Reads metadata (caption text, hashtags, uploader), caption tracks when the platform
has them, and downloads the video. Nothing is ever uploaded back to a platform.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

PLATFORMS: list[tuple[str, re.Pattern[str]]] = [
    ("tiktok", re.compile(r"^(https?://)?([\w-]+\.)?(tiktok\.com|tiktokv\.com)/", re.I)),
    ("instagram", re.compile(r"^(https?://)?([\w-]+\.)?(instagram\.com|instagr\.am)/", re.I)),
    ("youtube", re.compile(r"^(https?://)?([\w-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)),
    ("x", re.compile(r"^(https?://)?([\w-]+\.)?(x\.com|twitter\.com)/", re.I)),
    ("facebook", re.compile(r"^(https?://)?([\w-]+\.)?(facebook\.com|fb\.watch)/", re.I)),
    ("snapchat", re.compile(r"^(https?://)?([\w-]+\.)?snapchat\.com/", re.I)),
    ("pinterest", re.compile(r"^(https?://)?([\w-]+\.)?(pinterest\.[a-z.]+|pin\.it)/", re.I)),
    ("vimeo", re.compile(r"^(https?://)?([\w-]+\.)?vimeo\.com/", re.I)),
    ("twitch", re.compile(r"^(https?://)?([\w-]+\.)?twitch\.tv/", re.I)),
    ("linkedin", re.compile(r"^(https?://)?([\w-]+\.)?linkedin\.com/", re.I)),
]

ID_PATTERNS = [
    r"/video/(\d{6,})",            # TikTok
    r"/(?:reel|reels|p|tv)/([\w-]{5,})",  # Instagram
    r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{6,})",  # YouTube
    r"/status/(\d{6,})",           # X / Twitter
    r"/(?:videos|watch/?\?v=|reel)/?(\d{6,})",  # Facebook
]


class SourceError(Exception):
    pass


# Kept for code shared with youtube-video-translator.
YouTubeError = SourceError


def is_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def platform_of(value: str) -> Optional[str]:
    """`tiktok`, `instagram`, `youtube`, `x`, ... for a URL; `web` for other URLs; None for local paths."""
    v = value.strip()
    if not is_url(v):
        return None
    for name, rx in PLATFORMS:
        if rx.match(v):
            return name
    return "web"


def is_youtube_url(value: str) -> bool:
    return platform_of(value) == "youtube"


def video_id(url: str) -> Optional[str]:
    for pat in ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


HASHTAG_RE = re.compile(r"(?<![\w&])#([^\s#.,!?;:()\[\]{}\"']+)", re.U)


def split_hashtags(text: str) -> tuple[str, list[str]]:
    """Caption text without hashtags, and the hashtags in order (deduplicated, without '#')."""
    tags: list[str] = []
    for t in HASHTAG_RE.findall(text or ""):
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    body = HASHTAG_RE.sub("", text or "")
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\s*\n\s*", "\n", body).strip()
    return body, tags


def _ydl(opts: dict[str, Any]):
    try:
        import yt_dlp  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise SourceError("yt-dlp is not installed: pip install yt-dlp") from exc
    base: dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}
    import os

    cookies = os.environ.get("YTDLP_COOKIES", "").strip()
    browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if cookies:
        base["cookiefile"] = os.path.expanduser(cookies)
    elif browser:
        base["cookiesfrombrowser"] = (browser,)
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def fetch_info(url: str) -> dict[str, Any]:
    """Video metadata (title, description, duration, language, caption tracks) without downloading."""
    try:
        with _ydl({"skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise SourceError(f"yt-dlp could not read {url}: {exc}{_hint(exc)}") from None
    if not info:
        raise SourceError(f"yt-dlp returned no metadata for {url}")
    return info


def _hint(exc: Exception) -> str:
    text = str(exc).lower()
    if "not a bot" in text or "sign in" in text or "cookies" in text:
        return (
            " | Hint: the platform wants a signed-in browser session from this network (common for Instagram and "
            "some TikTok/YouTube videos). Set "
            "YTDLP_COOKIES_FROM_BROWSER=chrome (or firefox, edge, safari) in the server's env, or "
            "YTDLP_COOKIES=/path/to/cookies.txt. Or download the video yourself and pass the file path."
        )
    if "unsupported url" in text:
        return " | Hint: yt-dlp does not support this site; download the video and pass the file path."
    if "private" in text or "login" in text:
        return (
            " | Hint: the video is private or login-only. Set YTDLP_COOKIES_FROM_BROWSER=chrome or pass a local file."
        )
    return ""


def pick_caption_track(info: dict[str, Any], language: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Best caption track: manual captions in `language` (or the video language), then automatic ones.

    Returns {"url", "ext", "language", "kind"} or None.
    """
    lang = (language or info.get("language") or "").lower() or None
    candidates: list[tuple[str, dict[str, Any]]] = [
        ("manual", info.get("subtitles") or {}),
        ("automatic", info.get("automatic_captions") or {}),
    ]
    for kind, tracks in candidates:
        if not tracks:
            continue
        keys = list(tracks)
        order: list[str] = []
        if lang:
            order += [k for k in keys if k.lower() == lang]
            order += [k for k in keys if k.lower().split("-")[0] == lang.split("-")[0] and k not in order]
            # automatic captions also list machine translations ("es-en"); prefer the "-orig" track
            order = sorted(order, key=lambda k: (0 if k.endswith("-orig") else 1, len(k)))
        elif kind == "automatic":
            order += [k for k in keys if k.endswith("-orig")]
        else:
            order += keys[:1]
        if not order and kind == "manual" and not lang:
            order = keys[:1]
        for key in order:
            formats = tracks.get(key) or []
            by_ext = {f.get("ext"): f for f in formats if f.get("url")}
            for ext in ("json3", "vtt", "srv3", "ttml", "srt"):
                if ext in by_ext and ext in ("json3", "vtt", "srt"):
                    return {"url": by_ext[ext]["url"], "ext": ext, "language": key.replace("-orig", ""), "kind": kind}
    return None


def download_text(url: str, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - caption URL from yt-dlp
        return resp.read().decode("utf-8", "replace")


def download_audio(url: str, dest_dir: Path) -> Path:
    """Download the best audio-only stream (no re-encode). Returns the file path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {"format": "bestaudio/best", "outtmpl": str(dest_dir / "source-audio.%(ext)s")}
    try:
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except Exception as exc:
        raise SourceError(f"yt-dlp could not download audio from {url}: {exc}{_hint(exc)}") from None
    if not path.exists():
        found = sorted(dest_dir.glob("source-audio.*"))
        if not found:
            raise SourceError("yt-dlp finished but no audio file was written.")
        path = found[0]
    return path


def download_video(url: str, dest_dir: Path, max_height: int = 1920) -> Path:
    """Download the best MP4 up to `max_height` (vertical 1080x1920 fits)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/b[height<={max_height}][ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source-video.%(ext)s"),
    }
    try:
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    except Exception as exc:
        raise SourceError(f"yt-dlp could not download video from {url}: {exc}{_hint(exc)}") from None
    if not path.exists():
        found = sorted(dest_dir.glob("source-video.*"))
        if not found:
            raise SourceError("yt-dlp finished but no video file was written.")
        path = found[0]
    return path
