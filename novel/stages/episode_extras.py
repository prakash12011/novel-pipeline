"""The subscribe cards every episode shares: one spoken message, one image, two clips.

* voiced card: the image for at least 5 s with the narrator asking viewers to subscribe and comment.
  Where it goes is video.subscribe.voiced.placement (episode_join).
* silent card: a Subscribe / Like / Comment card (video.subscribe.silent.seconds), after every
  video.subscribe.silent.every_chapters-th chapter.

The message is voiced once, on Kaggle, through the normal episode narration path. It lives in
a small pseudo-episode, E900 (chapter 9001), at <book>/extras/E900, so nothing of the real episodes is touched:

    novel extras voice                                  # writes the message, prints the next commands
    novel tts push --ep 900 --chapters 9001
    novel tts pull --ep 900
    novel extras cards                                  # E900/video/subscribe-voiced.mp4 + -silent.mp4

The words come from settings (channel.name, video.subscribe.card_text, video.subscribe.spoken_lines);
when those are empty the generic constants below are used. No Gemini call is needed: the old module
imported one but never used it (the Hindi was always written by hand, in SPOKEN).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import episode as E
from .episode_render import FPS, H, W
from .episode_video import card_fonts, logo_layer, logo_signature, paste_logo
from .manifest import Chapter, Manifest

EP = 900
CH = 9001
VOICED_MIN = 5.0       # seconds the card stays up at least

# The words on the card, one line each: heading, button, reactions, tagline. The channel name
# (channel.name) is drawn between the button and the reactions. {title} is book.title.
CARD_LINES = [
    "Enjoying the story?",
    "SUBSCRIBE",
    "Like  \u00b7  Comment  \u00b7  Share",
    "Help other {title} fans find this channel",
]

# What the narrator says, one paragraph per line: the narration puts a short pause after each, so it
# sounds like a person talking, not a read-out. Replaced by video.subscribe.spoken_lines when set.
SPOKEN = [
    "अगर आपको यह एपिसोड पसंद आ रहा है, तो कृपया चैनल को सपोर्ट करने के लिए सब्सक्राइब करें!",
    "इस वीडियो को लाइक करें और अपनी थ्योरीज़, फेवरेट किरदार, या विचारों के साथ एक कमेंट छोड़ें।",
    "आपके कमेंट्स से दूसरे फैंस को इस चैनल को खोजने और हमारी कम्युनिटी से जुड़ने में मदद मिलती है।",
    "आइए मिलकर रहस्यों की खोज करें!",
]


def spoken_lines(cfg: dict | None = None) -> list[str]:
    cfg = cfg or E.cfg()
    lines = cfg["video"]["subscribe"].get("spoken_lines") or SPOKEN
    return [str(x).strip() for x in lines if str(x).strip()]


def card_lines(cfg: dict) -> list[str]:
    raw = cfg["video"]["subscribe"].get("card_text")
    lines = raw.splitlines() if isinstance(raw, str) else list(raw or CARD_LINES)
    lines = [str(x).strip() for x in lines if str(x).strip()]
    lines += [""] * (4 - len(lines))                     # fewer than 4 lines: the rest stay empty
    title = (cfg.get("book") or {}).get("title") or "the novel"
    return [x.replace("{title}", title) for x in lines[:4]]


def setup_message() -> Path:
    root = E.root(EP)
    for sub in ("hindi", "chapters", "qa"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    cfg = E.cfg()
    text = "\n\n".join(spoken_lines(cfg))
    hindi = root / "hindi" / f"{CH:05d}.txt"
    hindi.write_text(text + "\n", encoding="utf-8")
    src = f"{CH:04d}_subscribe.txt"
    (root / "chapters" / src).write_text("Subscribe\n", encoding="utf-8")
    Manifest(path=root / "manifest.json",
             chapters=[Chapter(idx=CH, title="Subscribe", source=src)]).save()
    (root / "episode.json").write_text(json.dumps({
        "episode": EP, "start": CH, "end": CH, "extra": True, "voice": cfg["tts"]["voice"],
        "note": "Series-wide extras (subscribe message). Not a real episode."}, indent=2), encoding="utf-8")
    E.lock_path(root).write_text(json.dumps({hindi.name: E.sha256(hindi)}, indent=1), encoding="utf-8")
    print(f"message ({len(text.split())} words): {text}\n-> {hindi}")
    print("next: novel tts push --ep 900 --chapters 9001\n"
          "      novel tts pull --ep 900\n"
          "      novel extras cards")
    return hindi


def card_image(out: Path, name: str = "subscribe", cfg: dict | None = None) -> None:
    """Blurred, darkened story picture behind a subscribe button, in English so any font works."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    cfg = cfg or E.cfg()
    mine = E.root(EP) / "image" / f"{name}.jpg"            # a Flow picture with the text already on it
    if mine.exists():
        from .episode_video import clean_image
        clean_image(mine, out.with_suffix(".clean.jpg"))     # crops Flow's mark off
        pic = Image.open(out.with_suffix(".clean.jpg")).convert("RGB").resize((W, H), Image.LANCZOS)
        paste_logo(pic, logo_layer(cfg["video"]["logo"], (W, H)))
        pic.save(out)
        return
    bg = None
    for p in sorted(E.root(1).glob("images/c0001/hd/s*.jpg"))[:1] + sorted(E.EPISODES.glob("E*/images/c0001/hd/s*.jpg"))[:1]:
        bg = Image.open(p).convert("RGB").resize((W, H))
        break
    img = (bg.filter(ImageFilter.GaussianBlur(14)) if bg else Image.new("RGB", (W, H), (20, 20, 28)))
    shade = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.blend(img, shade, 0.62)
    d = ImageDraw.Draw(img)

    regular, bold = card_fonts(cfg)

    def font(size, is_bold=False):
        return ImageFont.truetype(str(bold if is_bold else regular), size)

    heading, button, reactions, tagline = card_lines(cfg)
    d.text((W / 2, 250), heading, font=font(74, True), fill=(255, 255, 255), anchor="mm")
    # the subscribe button
    bw, bh = 560, 130
    x0, y0 = (W - bw) / 2, 380
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=22, fill=(212, 28, 28))
    d.text((W / 2, y0 + bh / 2), button, font=font(66, True), fill=(255, 255, 255), anchor="mm")
    d.text((W / 2, 590), cfg["channel"]["name"], font=font(48, True), fill=(255, 224, 160), anchor="mm")
    d.text((W / 2, 690), reactions, font=font(44), fill=(255, 255, 255), anchor="mm")
    d.text((W / 2, 800), tagline, font=font(34), fill=(220, 220, 220), anchor="mm")
    paste_logo(img, logo_layer(cfg["video"]["logo"], (W, H)))
    img.save(out)


def _clip(png: Path, out: Path, seconds: float, audio: Path | None, rate: int) -> None:
    from .episode_video import run
    cmd = ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", png]
    if audio:
        cmd += ["-i", audio]
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=mono"]
    af = f"adelay=300:all=1,apad,aresample={rate}" if audio else f"aresample={rate}"
    cmd += ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                   f"fade=t=in:st=0:d=0.3,fade=t=out:st={seconds - 0.4:.2f}:d=0.4",
            "-af", af, "-ac", "1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-r", str(FPS),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:a", "aac", "-b:a", "160k", "-t", f"{seconds:.3f}", "-movflags", "+faststart", out]
    p = run(cmd)
    if p.returncode:
        sys.exit(f"card clip failed: {p.stderr[-500:]}")


def _signature(cfg: dict) -> str:
    """What the cards are made from besides the narration: words, channel, seconds, fonts, logo."""
    fonts = [(str(f), f.stat().st_mtime_ns) for f in card_fonts(cfg)]
    blob = json.dumps([card_lines(cfg), cfg["channel"]["name"], float(cfg["video"]["subscribe"]["silent"]["seconds"]),
                       fonts, logo_signature(cfg["video"]["logo"])], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_cards(rate: int = 24000) -> dict[str, Path]:
    from .episode_video import probe
    cfg = E.cfg()
    silent_s = float(cfg["video"]["subscribe"]["silent"]["seconds"])
    root = E.root(EP)
    mp3 = root / "audio" / f"{CH:05d}.mp3"
    if not mp3.exists():
        sys.exit(f"no narration yet ({mp3}): run the message, push, pull steps first")
    vid = root / "video"
    vid.mkdir(exist_ok=True)
    png, png_short = vid / "subscribe.png", vid / "subscribe-short.png"
    card_image(png, cfg=cfg)
    card_image(png_short, "subscribe-short", cfg)           # the 2 s card: its own picture if there is one, else the same
    voice = float(probe(mp3)["format"]["duration"])
    seconds = max(VOICED_MIN, voice + 0.3 + 0.9)       # a little air before and after the voice
    voiced, silent = vid / "subscribe-voiced.mp4", vid / "subscribe-silent.mp4"
    _clip(png, voiced, seconds, mp3, rate)
    _clip(png_short, silent, silent_s, None, rate)
    (vid / "cards.json").write_text(json.dumps({"key": _signature(cfg)}), encoding="utf-8")
    print(f"voiced card {seconds:.1f} s (voice {voice:.1f} s), silent card {silent_s:.0f} s -> {vid}")
    return {"voiced": voiced, "silent": silent}


def cards_ready(rate: int = 24000) -> dict[str, Path] | None:
    """The two clips, rebuilt if the narration or this module's look changed; None if not narrated yet."""
    root = E.root(EP)
    mp3 = root / "audio" / f"{CH:05d}.mp3"
    if not mp3.exists():
        return None
    voiced = root / "video" / "subscribe-voiced.mp4"
    pics = [q for q in (root / "image").glob("subscribe*.jpg")]
    newest = max([mp3.stat().st_mtime, Path(__file__).stat().st_mtime] + [q.stat().st_mtime for q in pics])
    stamp = root / "video" / "cards.json"
    current = stamp.exists() and json.loads(stamp.read_text(encoding="utf-8")).get("key") == _signature(E.cfg())
    if not voiced.exists() or voiced.stat().st_mtime < newest or not current:
        return make_cards(rate)
    return {"voiced": voiced, "silent": root / "video" / "subscribe-silent.mp4"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("message")
    sub.add_parser("cards")
    a = ap.parse_args()
    setup_message() if a.cmd == "message" else make_cards()


if __name__ == "__main__":
    main()
