"""tiktok-ai-dubbing: AI dubbing and voiceover for TikTok, Instagram Reels and YouTube Shorts.

    tiktok-ai-dubbing "https://www.tiktok.com/@user/video/7300000000000000000" --to en
    tiktok-ai-dubbing reel.mp4 --to es,pt-BR --translation reel.es.srt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__

EPILOG = """examples:
  tiktok-ai-dubbing "https://www.tiktok.com/@creator/video/7300000000000000000" --to en
  tiktok-ai-dubbing "https://www.instagram.com/reel/REEL_ID/" --to es,de
  tiktok-ai-dubbing "https://www.youtube.com/shorts/VIDEO_ID" --to ja --platform shorts
  tiktok-ai-dubbing "https://x.com/user/status/1800000000000000000" --to en --caption-position middle
  tiktok-ai-dubbing clip.mp4 --to en --translation clip.en.srt
  tiktok-ai-dubbing clip.mp4 --to en --voice "Speaker 1=https://example.com/me.wav|What I say in the clip"
  tiktok-ai-dubbing clip.mp4 --to fr --subtitles-only

outputs (in ./dubbed/<project>/):
  <title>.<lang>.mp4        1080x1920 dubbed video, captions burned in (word highlight, safe zone)
  <title>.<lang>.srt        captions timed to the dub (upload as closed captions)
  <title>.<lang>.ass        the styled captions, for CapCut / Premiere / DaVinci or --no-burn
  <title>.<lang>.post.txt   translated post caption + hashtags + credit line, ready to paste
  <title>.<lang>.mp3        the dubbed voice track

environment:
  MIRAGEA_API_KEY            voice engine key (voice cloning, dubbing, speech recognition)
  TRANSLATOR_LLM_BASE_URL    any OpenAI-compatible endpoint, e.g. https://api.openai.com/v1,
                             https://openrouter.ai/api/v1, http://localhost:11434/v1 (Ollama)
  TRANSLATOR_LLM_API_KEY     its key (falls back to OPENAI_API_KEY)
  TRANSLATOR_LLM_MODEL       model name (default gpt-4o-mini)
  YTDLP_COOKIES_FROM_BROWSER chrome|firefox|edge|safari, when Instagram/TikTok ask to sign in

Run the same command again to resume: finished steps are reused, nothing is submitted twice.
Docs: https://github.com/RTY2345RR/tiktok-ai-dubbing
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tiktok-ai-dubbing",
        description="TikTok AI dubbing and voiceover CLI: dub TikTok, Instagram Reels, YouTube Shorts and X videos "
        "into another language in each speaker's cloned voice, as a 9:16 MP4 with word-highlight captions, "
        "SRT and a translated post caption with hashtags. No GPU needed.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source", nargs="?", help="TikTok / Reels / Shorts / X / Facebook video URL, a local video file, or an SRT/VTT")
    p.add_argument("--to", dest="targets", help="target language code(s), comma separated: en, es, de, pt-BR, ja...")
    p.add_argument("--from", dest="source_language", help="source language code (auto-detected when omitted)")
    p.add_argument("-t", "--translation", dest="translation_file",
                   help="your own translation: SRT/VTT, TXT (one line per segment) or JSON {id: line} / {lang: {id: line}}")
    p.add_argument("--llm-base-url", help="OpenAI-compatible endpoint for machine translation (overrides TRANSLATOR_LLM_BASE_URL)")
    p.add_argument("--llm-model", help="LLM model for translation (overrides TRANSLATOR_LLM_MODEL)")
    p.add_argument("--voice", action="append", default=[], metavar="NAME=VOICE",
                   help='set a speaker voice instead of cloning it from the video: "NAME=URL", "NAME=URL|clip transcript", '
                   '"NAME=actor:ID"; NAME "*" means every speaker. Repeatable')
    p.add_argument("--speakers", dest="num_speakers", type=int, help="number of speakers (default: auto)")
    p.add_argument("--captions", dest="captions_file", help="use this SRT/VTT as the source transcript")
    p.add_argument("--no-platform-captions", action="store_true", help="ignore the platform's own captions and run speech recognition")

    v = p.add_argument_group("short-form video")
    v.add_argument("--platform", choices=("all", "tiktok", "reels", "shorts"), default="all",
                   help="whose on-screen buttons to keep captions clear of (default all = the strictest)")
    v.add_argument("--caption-position", choices=("lower", "middle", "upper"), default="lower",
                   help="where captions sit inside the safe zone (default lower)")
    v.add_argument("--words", dest="words_per_caption", type=int, default=3, help="words on screen at a time, 1-6 (default 3)")
    v.add_argument("--caption-case", choices=("as-is", "upper"), default="as-is", help="upper = ALL CAPS captions")
    v.add_argument("--font", default="Arial", help="caption font name (default Arial; any installed font)")
    v.add_argument("--font-size", type=int, help="caption size in px (default 6.2%% of the frame height)")
    v.add_argument("--fonts-dir", help="folder with .ttf/.otf files for --font (e.g. a downloaded Montserrat)")
    v.add_argument("--framing", choices=("auto", "blur", "crop", "pad"), default="auto",
                   help="how non-vertical videos become 9:16: blurred background, centre crop or bars (default auto)")
    v.add_argument("--resolution", default="1080x1920", help="output size (default 1080x1920; 720x1280 is faster)")
    v.add_argument("--no-burn", action="store_true", help="do not burn captions into the video (keep SRT/ASS only)")
    v.add_argument("--no-credit", action="store_true", help="leave the 'Original video by @creator' line out of the post text")

    p.add_argument("--subtitles-only", action="store_true", help="only write translated SRT subtitles (no voices)")
    p.add_argument("--audio-only", action="store_true", help="only the dubbed voice track, no video")
    p.add_argument("--format", dest="audio_format", choices=("mp3", "m4a", "wav"), default="mp3", help="voice track format (default mp3)")
    p.add_argument("--max-speedup", type=float, default=1.25, help="max speed-up for lines longer than their slot (default 1.25)")
    p.add_argument("--original-volume", type=float, default=0.0,
                   help="keep the original sound (music, effects) under the dub at this volume, 0-1 (default 0)")
    p.add_argument("--model", help="voice model id (see --check / the voice engine's capabilities)")
    p.add_argument("-o", "--output", dest="output_dir", help="output folder (default ./dubbed)")
    p.add_argument("--fresh", action="store_true", help="start over instead of resuming the saved project")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress messages")
    p.add_argument("--check", action="store_true", help="check ffmpeg, yt-dlp, the voice engine key and the LLM settings")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _print_summary(result: dict) -> None:
    print(f"\nDone: {result['title']}  ({result['source_language']} -> "
          f"{', '.join(r['language'] for r in result['languages'])})")
    print(f"  speakers: {', '.join(result['speakers'])}   transcript: {result['transcript_origin']}")
    for r in result["languages"]:
        print(f"  [{r['language']}]")
        for key in ("video", "subtitles", "post_text", "audio", "captions_ass"):
            if r.get(key):
                print(f"    {key:12} {r[key]}")
        if r.get("overflowing_lines"):
            print(f"    note         {r['advice']}")
    print(f"  project: {result['project']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from .translator import Options, check, run

    if args.check:
        report = check(args.llm_base_url, args.llm_model)
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 2
    if not args.source or not args.targets:
        parser.error("give a SOURCE and --to LANG (for example: tiktok-ai-dubbing URL --to en)")
    if not 1.0 <= args.max_speedup <= 2.0:
        parser.error("--max-speedup must be between 1.0 and 2.0")
    if not 0.0 <= args.original_volume <= 1.0:
        parser.error("--original-volume must be between 0 and 1")
    if not 1 <= args.words_per_caption <= 6:
        parser.error("--words must be between 1 and 6")
    opts = Options(
        source=args.source,
        targets=[t.strip() for t in args.targets.split(",") if t.strip()],
        source_language=args.source_language,
        translation_file=args.translation_file,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        voices=args.voice,
        num_speakers=args.num_speakers,
        captions_file=args.captions_file,
        use_platform_captions=not args.no_platform_captions,
        subtitles_only=args.subtitles_only,
        audio_only=args.audio_only,
        audio_format=args.audio_format,
        max_speedup=args.max_speedup,
        original_volume=args.original_volume,
        model=args.model,
        output_dir=args.output_dir,
        fresh=args.fresh,
        quiet=args.quiet or args.json,
        burn_captions=not args.no_burn,
        caption_position=args.caption_position,
        platform=args.platform,
        words_per_caption=args.words_per_caption,
        caption_case=args.caption_case,
        font=args.font,
        font_size=args.font_size,
        fonts_dir=args.fonts_dir,
        framing=args.framing,
        resolution=args.resolution,
        credit=not args.no_credit,
    )
    try:
        result = asyncio.run(run(opts))
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to resume.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - every error becomes one readable line
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
