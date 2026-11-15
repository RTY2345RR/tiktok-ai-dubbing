import re
from pathlib import Path

import anyio
import pytest

from tiktok_ai_dubbing import shortform, sources, translator
from tiktok_ai_dubbing.media import probe_duration, probe_video_size, run_ffmpeg
from tiktok_ai_dubbing.subtitles import Segment


def test_platform_detection_and_ids():
    assert sources.platform_of("https://www.tiktok.com/@a/video/7312345678901234567") == "tiktok"
    assert sources.platform_of("https://vm.tiktok.com/ZMabc123/") == "tiktok"
    assert sources.platform_of("https://www.instagram.com/reel/C1a2B3c4D5e/") == "instagram"
    assert sources.platform_of("https://youtube.com/shorts/abcdefghijk") == "youtube"
    assert sources.platform_of("https://x.com/u/status/1800000000000000000") == "x"
    assert sources.platform_of("https://fb.watch/abc/") == "facebook"
    assert sources.platform_of("https://example.com/v.mp4") == "web"
    assert sources.platform_of("clip.mp4") is None
    assert sources.video_id("https://x.com/u/status/1800000000000000000") == "1800000000000000000"


def test_split_hashtags_keeps_order_and_dedupes():
    body, tags = sources.split_hashtags("Receta fácil 🍝 #pasta #recetas #Pasta\n#fyp")
    assert body == "Receta fácil 🍝" and tags == ["pasta", "recetas", "fyp"]


def test_word_timing_fills_the_line_and_chunks_respect_limits():
    words = shortform.time_words(Segment(1, 1.0, 4.0, "Hello everyone, this is a quick test of captions."))
    assert words[0].start == 1.0 and abs(words[-1].end - 4.0) < 1e-6
    assert all(a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))
    chunks = shortform.chunk_words(words, 3, 22)
    assert all(len(c) <= 3 for c in chunks)
    assert [w.text for w in chunks[0]] == ["Hello", "everyone,", "this"]


def test_cjk_lines_are_split_without_spaces():
    assert shortform.is_spaceless("今天我们试一下配音。")
    units = shortform.split_words("今天我们试一下配音。")
    assert "".join(units) == "今天我们试一下配音。" and all(len(u) <= 3 for u in units)


def test_ass_highlights_active_word_per_speaker_inside_safe_zone():
    style = shortform.CaptionStyle(case="upper")
    segs = [Segment(1, 0.0, 2.0, "Hi there friends", "Ana"), Segment(2, 2.2, 4.0, "Welcome back", "Ben")]
    ass = shortform.build_ass(segs, style, ["Ana", "Ben"])
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 5  # one event per spoken word
    assert "{\\c&H0000E6FF\\fscx112\\fscy112}HI{\\r} THERE" in events[0]
    assert "&H00FFF53D" in events[3]  # second speaker gets the second colour
    left, top, right, bottom = shortform.safe_box(style)
    m = re.search(r"Style: Short,.*,(\d+),(\d+),(\d+),(\d+),1$", ass, re.M)
    align, ml, mr, mv = map(int, m.groups())
    assert align == 2 and ml == left and 1080 - mr == right and mv == 1920 - bottom
    assert bottom <= 1920 * 0.75 and right <= 1080 * 0.84
    t = [l.split(",")[1:3] for l in events]
    assert t[0][0] == "0:00:00.00"


def test_style_validation():
    with pytest.raises(ValueError):
        shortform.CaptionStyle(position="side")
    with pytest.raises(ValueError):
        shortform.CaptionStyle(max_words=9)


@pytest.mark.parametrize("framing,src", [("auto", (160, 90)), ("crop", (90, 160)), ("pad", (160, 160))])
def test_render_vertical_any_aspect(tmp_path, framing, src):
    w, h = src
    video = tmp_path / "in.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", f"testsrc=s={w}x{h}:d=2", "-c:v", "mpeg4", str(video)])
    audio = tmp_path / "dub.mp3"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=300:duration=2.6", "-c:a", "libmp3lame", str(audio)])
    style = shortform.CaptionStyle(width=270, height=480)
    ass = tmp_path / "c.ass"
    ass.write_text(shortform.build_ass([Segment(1, 0.2, 2.0, "Short captions here")], style), encoding="utf-8")
    out = shortform.render_vertical(video, audio, tmp_path / "out" / "o.mp4", ass=ass, width=270, height=480, framing=framing)
    assert probe_video_size(out) == (270, 480)
    assert probe_duration(out) >= 2.5  # last frame held until the dub ends


def test_post_text_and_checks():
    text = shortform.post_text("Easy pasta 🍝", ["pasta", "#recipe"], "Original video by @chef: https://t.test/1")
    assert text == "Easy pasta 🍝\n\n#pasta #recipe\n\nOriginal video by @chef: https://t.test/1\n"
    c = shortform.post_checks(text, ["pasta", "recipe"])
    assert c["fits_tiktok"] and c["fits_instagram"] and c["fits_x"]


def test_url_source_uses_metadata_hashtags_and_translates_post(engine, llm, sample_host, speech_video, monkeypatch, tmp_path):
    info = {
        "id": "7312345678901234567", "title": "Receta fácil", "description": "Receta fácil de pasta #pasta #recetas",
        "uploader": "Chef Ana", "uploader_id": "chefana", "webpage_url": "https://www.tiktok.com/@chefana/video/7312345678901234567",
        "duration": 9, "width": 160, "height": 90, "tags": ["cocina"],
    }

    def fake_download(url, dest):
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / "source-video.mp4"
        p.write_bytes(Path(speech_video).read_bytes())
        return p

    monkeypatch.setattr(sources, "fetch_info", lambda url: info)
    monkeypatch.setattr(sources, "download_video", fake_download)
    o = translator.Options(source=info["webpage_url"], targets=["en"], quiet=True, resolution="360x640")
    res = anyio.run(translator.run, o)
    assert res["platform"] == "tiktok" and res["project"].endswith(tuple("0123456789abcdef"))
    row = res["languages"][0]
    post = Path(row["post_text"]).read_text(encoding="utf-8")
    assert row["post_caption_translated"] and "EN: Receta fácil de pasta" in post
    assert "Original video by @chefana: https://www.tiktok.com/@chefana/video/7312345678901234567" in post
    assert probe_video_size(Path(row["video"])) == (360, 640)
    calls = len(llm.requests)
    anyio.run(translator.run, o)  # resume: nothing re-translated, no new voice jobs
    assert len(llm.requests) == calls
