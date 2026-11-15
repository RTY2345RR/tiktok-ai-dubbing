"""Short-form output: 9:16 framing, word-highlight captions in the TikTok safe zone, post caption text.

* ``build_ass`` turns the timed dub lines into an ASS subtitle file: 1-4 words on
  screen at a time, the word being spoken highlighted (one colour per speaker),
  kept inside the area that TikTok, Instagram Reels and YouTube Shorts leave free
  of their buttons, caption and progress bar.
* ``render_vertical`` frames any source (vertical, square or landscape) as a
  1080x1920 H.264 MP4 with the dubbed audio and burns the captions in.
* ``post_text`` writes the caption + hashtags file to paste when posting.

Word timings are estimated inside each dubbed line (the voice engine renders one
clip per line and we know exactly where each clip sits), weighted by word length.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import media
from .subtitles import Segment

# ------------------------------------------------------------------ safe zones
# Fractions of the frame that the platform UI covers on a 9:16 phone screen
# (measured on 1080x1920 screenshots of each app, rounded up to be safe).
SAFE_ZONES = {
    #            top    bottom  left   right
    "tiktok":   (0.13, 0.25, 0.05, 0.16),  # tabs on top; caption, sound, nav bar below; like/comment/share rail right
    "reels":    (0.11, 0.24, 0.05, 0.15),
    "shorts":   (0.10, 0.22, 0.05, 0.15),
    "all":      (0.13, 0.25, 0.05, 0.16),  # the strictest of the three
}
POSITIONS = ("lower", "middle", "upper")
HIGHLIGHTS = ["#FFE600", "#3DF5FF", "#FF5FD2", "#7CFF4F", "#FF9F1C", "#B79CFF"]  # one per speaker
CASES = ("as-is", "upper")


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        raise ValueError(f"Colour must look like #RRGGBB, got {hex_color!r}")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


# ----------------------------------------------------------------------- words
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\u0e00-\u0e7f]")


def is_spaceless(text: str) -> bool:
    """True for Chinese, Japanese, Thai... (no spaces between words)."""
    letters = [c for c in text if not c.isspace()]
    return bool(letters) and sum(1 for c in letters if _CJK.match(c)) / len(letters) > 0.5


def split_words(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if is_spaceless(text):
        # one "word" per 1-2 characters, punctuation glued to the previous unit
        units: list[str] = []
        for ch in text.replace(" ", ""):
            if units and (unicodedata.category(ch).startswith("P") or len(units[-1]) < 2 and not
                          unicodedata.category(units[-1][-1]).startswith("P")):
                units[-1] += ch
            else:
                units.append(ch)
        return units
    return text.split(" ")


@dataclass
class Word:
    text: str
    start: float
    end: float


def time_words(seg: Segment) -> list[Word]:
    """Spread a line's words over its spoken time, weighted by length (+ pauses after punctuation)."""
    words = split_words(seg.text)
    if not words:
        return []

    def weight(w: str) -> float:
        core = len(re.sub(r"[^\w]", "", w)) or 1
        pause = 1.5 if re.search(r"[,.;:!?…，。！？、]$", w) else 0.0
        return core + 1.0 + pause

    total = sum(weight(w) for w in words)
    span = max(0.2, seg.end - seg.start)
    out, t = [], seg.start
    for w in words:
        d = span * weight(w) / total
        out.append(Word(w, t, t + d))
        t += d
    return out


def chunk_words(words: list[Word], max_words: int, max_chars: int) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        joined = len(" ".join(x.text for x in cur + [w]))
        if cur and (len(cur) >= max_words or joined > max_chars):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…。！？]$", w.text):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


# ----------------------------------------------------------------------- ASS
@dataclass
class CaptionStyle:
    width: int = 1080
    height: int = 1920
    font: str = "Arial"
    size: Optional[int] = None  # px; default 6.2 % of the frame height
    position: str = "lower"
    platform: str = "all"
    max_words: int = 3
    case: str = "as-is"
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    highlights: tuple[str, ...] = tuple(HIGHLIGHTS)
    pop: bool = True  # scale the active word up a little

    def __post_init__(self) -> None:
        if self.position not in POSITIONS:
            raise ValueError(f"caption position must be one of {POSITIONS}")
        if self.platform not in SAFE_ZONES:
            raise ValueError(f"platform must be one of {tuple(SAFE_ZONES)}")
        if self.case not in CASES:
            raise ValueError(f"caption case must be one of {CASES}")
        if not 1 <= self.max_words <= 6:
            raise ValueError("words per caption must be 1-6")


def safe_box(style: CaptionStyle) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) pixel box that no platform button covers."""
    top, bottom, left, right = SAFE_ZONES[style.platform]
    w, h = style.width, style.height
    return int(w * left), int(h * top), int(w * (1 - right)), int(h * (1 - bottom))


def build_ass(segments: Iterable[Segment], style: CaptionStyle, speakers: Optional[list[str]] = None) -> str:
    segs = [s for s in segments if s.text.strip()]
    w, h = style.width, style.height
    size = style.size or int(round(h * 0.062 if w / h < 0.8 else h * 0.075))
    left, top, right, bottom = safe_box(style)
    spaceless = any(is_spaceless(s.text) for s in segs)
    max_chars = max(6, int((right - left) / (size * (0.95 if spaceless else 0.58))))
    if style.position == "lower":
        align, margin_v = 2, h - bottom
    elif style.position == "upper":
        align, margin_v = 8, top + int(h * 0.04)
    else:
        align, margin_v = 5, 0
    outline = max(3, size // 12)
    header = [
        "[Script Info]",
        "; Word-highlight short-form captions written by tiktok-ai-dubbing",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Short,{style.font},{size},{_ass_color(style.text_color)},{_ass_color(style.text_color)},"
        f"{_ass_color(style.outline_color)},{_ass_color('#000000', 0x60)},-1,0,0,0,100,100,0,0,1,{outline},"
        f"{max(1, outline // 2)},{align},{left},{w - right},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    order = list(dict.fromkeys((speakers or []) + [s.speaker or "Speaker 1" for s in segs]))
    events: list[str] = []
    all_chunks: list[tuple[list[Word], str]] = []
    for seg in segs:
        color = style.highlights[order.index(seg.speaker or "Speaker 1") % len(style.highlights)]
        for chunk in chunk_words(time_words(seg), style.max_words, max_chars):
            all_chunks.append((chunk, color))
    joiner = "" if spaceless else " "
    for ci, (chunk, color) in enumerate(all_chunks):
        nxt = all_chunks[ci + 1][0][0].start if ci + 1 < len(all_chunks) else None
        hold_until = chunk[-1].end + 0.25
        if nxt is not None:
            hold_until = min(hold_until, nxt) if nxt - chunk[-1].end < 0.6 else hold_until
            hold_until = min(hold_until, nxt)
        texts = [x.text.upper() if style.case == "upper" else x.text for x in chunk]
        for wi, word in enumerate(chunk):
            start = word.start
            end = chunk[wi + 1].start if wi + 1 < len(chunk) else hold_until
            if end - start < 0.02:
                continue
            parts = []
            for k, t in enumerate(texts):
                if k == wi:
                    pop = r"\fscx112\fscy112" if style.pop else ""
                    parts.append(f"{{\\c{_ass_color(color)}{pop}}}{_escape(t)}{{\\r}}")
                else:
                    parts.append(_escape(t))
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Short,,0,0,0,,{joiner.join(parts)}")
    return "\n".join(header + events) + "\n"


# ----------------------------------------------------------------- 9:16 render
FRAMINGS = ("auto", "blur", "crop", "pad")


def frame_filter(src_w: int, src_h: int, width: int, height: int, framing: str) -> str:
    """ffmpeg filtergraph from [0:v] to [framed] at width x height."""
    if framing not in FRAMINGS:
        raise ValueError(f"framing must be one of {FRAMINGS}")
    target, source = width / height, src_w / src_h
    if framing == "auto":
        framing = "crop" if abs(source - target) / target < 0.12 else "blur"
    fill = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease"
    if framing == "crop":
        return f"[0:v]{fill},setsar=1[framed]"
    if framing == "pad":
        return f"[0:v]{fit},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[framed]"
    return (
        f"[0:v]split=2[bgsrc][fgsrc];[bgsrc]scale={width // 4}:{height // 4}:force_original_aspect_ratio=increase,"
        f"crop={width // 4}:{height // 4},boxblur=8:2,scale={width}:{height},eq=brightness=-0.08[bg];"
        f"[fgsrc]{fit}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[framed]"
    )


def render_vertical(
    video: Path,
    audio: Path,
    dest: Path,
    *,
    ass: Optional[Path] = None,
    width: int = 1080,
    height: int = 1920,
    framing: str = "auto",
    fonts_dir: Optional[Path] = None,
    crf: int = 20,
) -> Path:
    """1080x1920 H.264 + AAC MP4 (+faststart) with the dub and burned-in captions."""
    src_w, src_h = media.probe_video_size(video)
    video_s = media.probe_duration(video)
    audio_s = media.probe_duration(audio)
    graph = frame_filter(src_w, src_h, width, height, framing)
    last = "framed"
    if audio_s > video_s + 0.05:  # hold the last frame while the dub finishes
        graph += f";[{last}]tpad=stop_mode=clone:stop_duration={audio_s - video_s + 0.1:.2f}[held]"
        last = "held"
    cwd = None
    if ass:
        cwd = ass.parent.resolve()  # relative name avoids ffmpeg filter escaping of Windows paths
        opt = f"ass={ass.name}"
        if fonts_dir:
            opt += ":fontsdir=" + str(Path(fonts_dir).resolve()).replace("\\", "/").replace(":", "\\:")
        graph += f";[{last}]{opt}[subbed]"
        last = "subbed"
    graph += f";[{last}]format=yuv420p[vout]"
    dest.parent.mkdir(parents=True, exist_ok=True)
    media.run_ffmpeg(
        [
            "-i", str(video.resolve()), "-i", str(audio.resolve()),
            "-filter_complex", graph, "-map", "[vout]", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-profile:v", "high",
            "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-t", f"{max(video_s, audio_s):.3f}", "-movflags", "+faststart", str(dest.resolve()),
        ],
        cwd=cwd,
    )
    return dest


# ----------------------------------------------------------------- post text
LIMITS = {"tiktok": 4000, "instagram": 2200, "youtube_shorts_title": 100, "x": 280}


def post_text(caption: str, hashtags: list[str], credit: Optional[str] = None) -> str:
    tags = " ".join("#" + re.sub(r"\s+", "", t.lstrip("#")) for t in hashtags if t.strip("# "))
    parts = [caption.strip()] if caption.strip() else []
    if tags:
        parts.append(tags)
    if credit:
        parts.append(credit)
    return "\n\n".join(parts) + "\n"


def post_checks(text: str, hashtags: list[str]) -> dict[str, object]:
    n = len(text.strip())
    return {
        "characters": n,
        "hashtags": len(hashtags),
        "fits_tiktok": n <= LIMITS["tiktok"],
        "fits_instagram": n <= LIMITS["instagram"] and len(hashtags) <= 30,
        "fits_x": n <= LIMITS["x"],
    }
