"""Hinglish retelling of an episode's chapters, with Gemini, checked before saving.

Built from gemini_batch.py (which stays as it is for the audiobook) plus what QA
found in its 83 chapters:

  * Gemini wrote Hindi placeholder notes - "(यह पैराग्राफ ऊपर वाले में शामिल कर
    दिया गया है)", "(खाली लाइन)" - in place of story text, and the narrator
    would have read them aloud. The cause was a 1:1 paragraph rule meeting
    English paragraphs split mid-sentence. So: split paragraphs are joined
    first, a paragraph may be dropped only with a machine-readable "<skip>",
    and a paragraph that is a bracketed note is rejected and redone.
  * Numbers are spoken as Hindi words, the way people talk ("तीन दिन में
    सिर्फ दो ब्रेड"). English counting was a workaround for IndicF5, which
    could not read digits; with Maya the listener heard "थ्री दिन में सिर्फ
    टू ब्रेड" and found it broke the story. A paragraph with digits is sent
    back to be written in words. Names that contain a number (फाइव सीज़,
    सेवन ऑफ वैंड्स) stay as the glossary has them.
  * Length alone can't show a plot point was dropped, so every chapter gets an
    audit call that compares the two texts.

This is a retelling at about 80% of the English length, not a line-by-line
translation, so paragraph N of the Hindi is NOT paragraph N of the English.
Nothing downstream needs it to be: scenes and timing are made from the saved
Hindi, which is exactly what gets narrated.

Usage:
    python -m abpipe.episode_translate run --episode 1 --chapters 1
    python -m abpipe.episode_translate run --episode 1 --chapters 2-20
    python -m abpipe.episode_translate run --episode 1 --chapters 1 --dry-run
    python -m abpipe.episode_translate lock --episode 1 --chapters 1-20
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import episode as E
from .manifest import Manifest, TRANSLATED
from .scenewrite import _extract_json, _retryable
from .spoken_numbers import ONES, TENS, speakable
from .translate import _glossary_block, load_glossary

MODEL = "gemini-3.5-flash-lite"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REPAIR_ROUNDS = 3
SKIP_CAP = 0.15                 # share of paragraphs that may be dropped
RATIO = (0.74, 0.88)            # Hindi/English characters: 0.8 x the 1.017 full-length median
KEY_WAIT_S = 65                 # every key rate-limited: wait out the per-minute window
KEY_WAIT_ROUNDS = 5             # ...this many times, then assume the daily quota is gone

# Copied from gemini_batch.py: this is the register the user chose.
HINGLISH = """Write in general Hinglish, the way educated Indians actually talk to friends every day: Hindi grammar, with the English word for anything people normally say in English in daily conversation, not only technical terms.
Examples: टाइम, रूम, आइडिया, प्लान, प्रॉब्लम, मून, क्वैरी, एक्चुअली, फाइनली, सिचुएशन. Write टाइम not समय, रूम not कमरा, मून not चंद्रमा, आइडिया not विचार.
Keep a Hindi word only when that is what people really say in conversation: घर, पानी, दरवाज़ा, अचानक.
Not this: अचानक उसे एक विचार आया और उसने समय देखा।
This: अचानक उसे एक आइडिया आया और उसने टाइम देखा।
It must feel like normal conversation, not a literary book. Spell every English word in Devanagari, because the narrator reads Devanagari."""

SYSTEM = """You retell a fantasy web novel in Hinglish for an audiobook narrator. Friends and family will listen, so it must be gripping and easy to follow.
{hinglish}
Retell, don't translate line by line. Keep every event, every piece of information the plot needs, and every line of dialogue that matters. Cut filler, repetition and over-long description. Aim for about 80% of the English length. Rebuild each sentence so it sounds natural spoken aloud.
Family members will listen. Keep sexual content and nudity non-explicit: say plainly what happened, for example that two people were secretly together or that someone was undressed, in one short sentence with no physical or sexual detail and no explicit dialogue. Everything else, including fights and danger, is retold as normal.
Write every number as Hindi words, the way people say them out loud: तीन दिन, दो ब्रेड, एक महीना, तेरह साल, सात बजे, तीस हज़ार. Never use digits. Only names that contain a number keep their fixed form, for example सेवन ऑफ वैंड्स or फाइव सीज़.
Every input paragraph starts with a marker like [12]. Start each output paragraph with the same marker, in the same order, one output paragraph per input paragraph.
If a paragraph adds nothing to the story, write its marker followed by <skip> and nothing else, for example: [14] <skip>. Do this rarely. Never skip [1], the chapter title.
Write [1] as: चैप्टर <number in Hindi words>: <title in Hinglish>, for example चैप्टर एक: फॉरेनर्स.
Output only the marked paragraphs. Never add notes, comments, or placeholder text of any kind.
{glossary}"""

# Asking the first pass for "80% of the length" does not work: paragraph-by-
# paragraph markers pull the model into a close translation (chapter 1 came
# back at 1.007 and 0.998 of the English). Condensing its own Hinglish does.
CONDENSE = """You tighten a Hinglish audiobook script. Keep its Hinglish style, every name, and every number word exactly as written.
Each paragraph starts with a marker like [12]. Rewrite every paragraph shorter, with the same marker, in the same order: cut filler words, repeated ideas and long descriptions, and merge sentences. Keep every event, every fact the story needs, and every line of dialogue that matters.
The whole text must come out about {cut}% shorter than it is now. A paragraph that is already one short sentence can stay as it is.
If a paragraph adds nothing, write its marker followed by <skip>. Never skip [1].
Output only the marked paragraphs. No notes or comments."""
CONDENSE_PASSES = 2

AUDIT = """You check a Hinglish retelling against the English chapter it retells.
The retelling is deliberately about 20% shorter: dropped filler, repetition and description are fine. It also deliberately removes explicit sexual detail and nudity while keeping the fact that something happened: do not list that detail as missing.
List only plot points that are missing or changed: an event, a fact a later chapter could need, a decision, a reveal, or a line of dialogue that matters. Do not list style, wording, or trimmed description.
Return JSON only: {"missing": [{"point": "<one English sentence>", "english_para": <number>}]}. Return {"missing": []} when nothing that matters is lost."""

MARKER = re.compile(r"\[(\d+)\]\s*")
SKIP = "<skip>"
# A paragraph left in English, or the model talking about its own output.
UNTRANSLATED = re.compile(r"(?:[A-Za-z'’-]+[\s,.]+){5}[A-Za-z]")
COMMENTARY = re.compile(r"(?i)\b(?:missing|skipped|paragraphs?|markers?|translation|translated)\b")
# Any Latin letter at all: "मिड-थर्टीज़" once came back as "थर्टी s", and the
# narrator would have read the s.
LATIN = re.compile(r"[A-Za-z]+")
DIGITS = re.compile(r"[0-9०-९]")
# English number words, reported for review (not rejected: वन is also "forest").
EN_NUMBER = re.compile(r"(?<![ऀ-ॿ])(?:" + "|".join(sorted(
    set(ONES) | {t for t in TENS if t} | {"हंड्रेड", "थाउजेंड"}, key=len, reverse=True))
    + r")(?![ऀ-ॿ])")
# The placeholder notes QA found: a whole paragraph in brackets that talks about
# paragraphs or lines. A story line that happens to start "नोट:" survives.
NOTE = re.compile(r"^\s*[(\[（](?=[^)\]）]*(?:पैराग्राफ|खाली|लाइन|शामिल|अनुवाद|छोड़|ऊपर वाले|पिछले))"
                  r"[^)\]）]*[)\]）]\s*[।.]?\s*$")
# Spelling drift seen in earlier output, normalised to the glossary's form.
VARIANTS = {
    "ल्यूमियन": "लुमियान", "लुमियन": "लुमियान", "लूमियन": "लुमियान",
    "डिमीगॉड": "डेमीगॉड", "डेमिगॉड": "डेमीगॉड", "वोर्टेक्स": "वर्टेक्स",
    "सीक्वेन्स": "सीक्वेंस", "सीक्वेंन्स": "सीक्वेंस",
}


# ------------------------------------------------------------------ text


def clean_paragraphs(text: str) -> list[str]:
    """English paragraphs, with sentences the epub split across <p> tags rejoined.

    A paragraph is joined to the next when the next starts in lowercase, or
    when it ends in a comma and the next does NOT open a quote. "He said," then
    a quoted line is ordinary dialogue and stays two paragraphs.
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    for p in paras:
        if out and (p[0].islower()
                    or (out[-1].endswith(",") and not p.startswith(('"', "“", "'", "‘")))):
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


def english_numbers(text: str, glossary: dict) -> list[str]:
    """English number words outside glossary names (फाइव सीज़ is a place)."""
    for name in sorted(glossary.values(), key=len, reverse=True):
        text = text.replace(name, " ")
    return EN_NUMBER.findall(text)


def apply_variants(text: str) -> str:
    for wrong, right in VARIANTS.items():
        text = text.replace(wrong, right)
    return text


def paragraph_problem(body: str) -> str | None:
    """Why a returned paragraph can't be used, or None."""
    if body == SKIP:
        return None
    if NOTE.match(body):
        return "note"
    if DIGITS.search(body):
        return "digits"
    if UNTRANSLATED.search(body) or COMMENTARY.search(body):
        return "english"
    if LATIN.search(body):
        return "latin"
    return None


def parse(raw: str, need: set[int]) -> dict[int, str]:
    """{paragraph number: text or <skip>} for every marked paragraph in a reply.

    Markers count anywhere in a line, and only in rising order, so a literal
    "[3]" inside paragraph 40 of the novel stays text instead of ending it.
    """
    found = []
    for m in MARKER.finditer(raw):
        i = int(m[1])
        if i in need and (not found or i > found[-1][0]):
            found.append((i, m.start(), m.end()))
    out = {}
    for (i, _, start), end in zip(found, [f[1] for f in found[1:]] + [len(raw)]):
        body = " ".join(raw[start:end].split())
        if body:
            out[i] = SKIP if body.strip("*_ ") == SKIP else body
    return out


def finish(body: str) -> str:
    """The exact text that will be narrated for one paragraph."""
    return apply_variants(body)


# ------------------------------------------------------------------ gemini


def api_keys() -> list[str]:
    from novel.core import paths, secrets
    keys = secrets.gemini_keys()
    if not keys:
        raise SystemExit(f"no GEMINI_API_KEY* in {paths.secrets_file()}")
    return keys


class Gemini:
    """REST calls that rotate keys on 429 and stop on errors that won't fix themselves."""

    def __init__(self, model: str, log: Path):
        self.model, self.keys, self.k, self.log = model, api_keys(), 0, log
        self.calls = 0

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            URL.format(model=self.model), json.dumps(body).encode(),
            {"x-goog-api-key": self.keys[self.k], "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=300))

    def ask(self, system: str, text: str, *, json_out: bool = False,
            temperature: float = 0.4) -> str:
        body = {"system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"temperature": temperature}}
        if json_out:
            body["generationConfig"]["responseMimeType"] = "application/json"
        limited = 0
        for attempt in range(1, 30):
            try:
                self.calls += 1
                r = self._post(body)
            except Exception as e:
                code = getattr(e, "code", None)
                self.note(event="http_error", code=code, key=self.k, error=str(e)[:200])
                if not _retryable(e):
                    raise SystemExit(f"Gemini refused with HTTP {code}: {e} - "
                                     f"this won't fix itself, stopping")
                if code == 429:
                    limited += 1
                    self.k = (self.k + 1) % len(self.keys)
                    if limited % len(self.keys) == 0:
                        if limited // len(self.keys) >= KEY_WAIT_ROUNDS:
                            raise SystemExit("every key is still rate-limited after "
                                             f"{KEY_WAIT_ROUNDS} waits - the daily quota is "
                                             "probably used up (it resets at midnight Pacific)")
                        print(f"    all {len(self.keys)} keys rate-limited, waiting "
                              f"{KEY_WAIT_S}s", flush=True)
                        time.sleep(KEY_WAIT_S)
                else:
                    time.sleep(min(60, 5 * attempt))
                continue
            if not r.get("candidates"):
                raise ValueError(f"blocked: {r.get('promptFeedback')}")
            cand = r["candidates"][0]
            if cand.get("finishReason") != "STOP":
                raise ValueError(f"finish={cand.get('finishReason')}")
            return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        raise SystemExit("Gemini kept failing - see the log")

    def note(self, **kw) -> None:
        kw["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(kw, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ chapter


def retell(g: Gemini, paras: list[str], glossary: dict, extra: str = "") -> tuple[dict, dict]:
    """Paragraph number -> finished text or <skip>, plus repair counts."""
    n = len(paras)
    system = SYSTEM.format(hinglish=HINGLISH,
                           glossary=_glossary_block(glossary, "\n".join(paras), limit=100))
    done: dict[int, str] = {}
    reasons: dict[str, int] = {}
    pending = list(range(1, n + 1))
    note = extra
    for rnd in range(1 + REPAIR_ROUNDS):
        if not pending:
            break
        raw = g.ask(system + note, "\n\n".join(f"[{i}] {paras[i - 1]}" for i in pending))
        got = parse(raw, set(pending))
        for i in pending:
            if i not in got:
                reasons["missing"] = reasons.get("missing", 0) + 1
                # A missing paragraph was usually swallowed by the one before
                # it, so that one is redone too, or its text would play twice.
                done.pop(i - 1, None)
                continue
            body = got[i]
            why = "title skipped" if (i == 1 and body == SKIP) else paragraph_problem(body)
            if why:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            done[i] = body if body == SKIP else finish(body)

        skips = [i for i, b in done.items() if b == SKIP]
        if len(skips) > max(1, int(SKIP_CAP * n)):
            reasons["too many skips"] = len(skips)
            for i in skips:
                done.pop(i)
        pending = [i for i in range(1, n + 1) if i not in done]
        note = ("\nThese paragraphs came back unusable before. Retell every one of them. "
                "Numbers as Hindi words, never digits. No notes. Use <skip> only if the "
                "paragraph truly adds nothing.")
    if pending:
        raise ValueError(f"{len(pending)} of {n} paragraphs still unusable after "
                         f"{REPAIR_ROUNDS} repairs: {pending[:10]} ({reasons})")
    return done, reasons


def condense(g: Gemini, done: dict[int, str], cut: int) -> tuple[dict[int, str], int]:
    """One tightening pass over the retold paragraphs.

    A paragraph is replaced only by a shorter version that passes every check;
    anything else keeps the text it had, so a bad pass can only fail to help.
    Returns the paragraphs and how many were shortened.
    """
    live = [i for i in sorted(done) if done[i] != SKIP]
    raw = g.ask(CONDENSE.format(cut=cut), "\n\n".join(f"[{i}] {done[i]}" for i in live))
    got = parse(raw, set(live))
    out, shortened = dict(done), 0
    cap = max(1, int(SKIP_CAP * len(done)))
    for i, body in got.items():
        if body == SKIP:
            if i != 1 and sum(b == SKIP for b in out.values()) < cap:
                out[i], shortened = SKIP, shortened + 1
            continue
        if paragraph_problem(body):
            continue
        body = finish(body)
        if len(body) >= len(done[i]):
            continue
        out[i], shortened = body, shortened + 1
    return out, shortened


def translate_chapter(g: Gemini, ep_root: Path, ch, glossary: dict) -> dict:
    t0 = time.time()
    english = (ep_root / "chapters" / ch.source).read_text(encoding="utf-8")
    paras = clean_paragraphs(english)
    en_chars = len(" ".join(paras))

    def measure(d: dict) -> tuple[list[int], str, float]:
        kept = [i for i in range(1, len(paras) + 1) if d[i] != SKIP]
        text = "\n\n".join(d[i] for i in kept)
        return kept, text, len(text) / en_chars

    done, reasons = retell(g, paras, glossary)
    kept, hindi, ratio = measure(done)
    first_ratio, passes = ratio, []
    target = (RATIO[0] + RATIO[1]) / 2
    for _ in range(CONDENSE_PASSES):
        if ratio <= RATIO[1]:
            break
        before = dict(done)
        done, shortened = condense(g, done, cut=round(100 * (1 - target / ratio)))
        kept, hindi, ratio = measure(done)
        # The model overshoots (asked for 19%, cut 34% on chapter 1). Give the
        # hardest-cut paragraphs their fuller version back until the chapter
        # reaches the target: detail returns exactly where most was taken.
        restored = 0
        for i in sorted((i for i in done if done[i] != before[i]),
                        key=lambda i: (len(done[i]) if done[i] != SKIP else 0) / len(before[i])):
            if ratio >= target:
                break
            done[i] = before[i]
            kept, hindi, ratio = measure(done)
            restored += 1
        passes.append({"shortened": shortened, "restored": restored, "ratio": round(ratio, 3)})
        g.note(event="condense", chapter=ch.idx, shortened=shortened, restored=restored,
               ratio=round(ratio, 3))

    if re.search(r"[0-9०-९]", hindi) or speakable(hindi) != hindi:
        raise ValueError("digits survived - the narrated text would differ from the saved text")

    audit_raw = g.ask(AUDIT, "ENGLISH:\n" + "\n\n".join(
        f"[{i}] {p}" for i, p in enumerate(paras, 1)) + "\n\nHINGLISH:\n" + hindi,
        json_out=True, temperature=0.0)
    try:
        missing = _extract_json(audit_raw).get("missing", [])
    except Exception:
        missing = [{"point": "audit reply was not JSON", "english_para": None}]

    qa = {
        "chapter": ch.idx, "model": g.model, "seconds": round(time.time() - t0, 1),
        "english_paragraphs": len(paras), "hindi_paragraphs": len(kept),
        "skipped": [i for i in range(1, len(paras) + 1) if done[i] == SKIP],
        # Hindi paragraph k (0-based) retells English paragraph source_paragraphs[k].
        "source_paragraphs": kept,
        "ratio": round(ratio, 3), "ratio_ok": RATIO[0] <= ratio <= RATIO[1],
        "first_pass_ratio": round(first_ratio, 3), "condense_passes": passes,
        "repairs": reasons,
        "english_numbers": english_numbers(hindi, glossary),
        "latin_words": LATIN.findall(hindi),
        "audit_missing": missing,
    }
    qa["pass"] = qa["ratio_ok"] and not qa["latin_words"] and not missing
    return {"hindi": hindi + "\n", "qa": qa}


# ------------------------------------------------------------------ commands


def parse_range(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def load_lock(ep_root: Path) -> dict:
    p = E.lock_path(ep_root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def cmd_run(args) -> None:
    ep_root = E.root(args.episode)
    man = Manifest.load(ep_root / "manifest.json")
    want = set(parse_range(args.chapters))
    todo = [c for c in man.chapters if c.idx in want]
    lock = load_lock(ep_root)
    locked = [c.idx for c in todo if f"{c.idx:05d}.txt" in lock]
    if locked:
        sys.exit(f"chapters {locked} are locked; their scenes and audio depend on the "
                 f"exact text. Unlock by editing {E.lock_path(ep_root)} on purpose.")
    glossary = load_glossary(ep_root / "glossary.json")
    if not glossary:
        sys.exit(f"{ep_root / 'glossary.json'} is missing - the cast comes first")

    if args.dry_run:
        c = todo[0]
        paras = clean_paragraphs((ep_root / "chapters" / c.source).read_text(encoding="utf-8"))
        print(SYSTEM.format(hinglish=HINGLISH,
                            glossary=_glossary_block(glossary, "\n".join(paras), limit=100)))
        print(f"\nchapter {c.idx}: {len(paras)} paragraphs after rejoining splits, "
              f"{len(' '.join(paras))} characters. Nothing sent.")
        return

    (ep_root / "hindi").mkdir(exist_ok=True)
    (ep_root / "qa").mkdir(exist_ok=True)
    g = Gemini(args.model, ep_root / "qa" / "log.jsonl")
    for ch in todo:
        print(f"chapter {ch.idx}: {ch.title}", flush=True)
        try:
            out = translate_chapter(g, ep_root, ch, glossary)
        except ValueError as e:
            g.note(event="failed", chapter=ch.idx, error=str(e))
            print(f"  FAILED: {e}", flush=True)
            continue
        name = f"{ch.idx:05d}"
        tmp = ep_root / "hindi" / f"{name}.tmp"   # a crash must not leave a half file
        tmp.write_text(out["hindi"], encoding="utf-8")
        tmp.replace(ep_root / "hindi" / f"{name}.txt")
        (ep_root / "qa" / f"{name}.json").write_text(
            json.dumps(out["qa"], ensure_ascii=False, indent=1), encoding="utf-8")
        ch.hindi, ch.status = f"{name}.txt", TRANSLATED
        ch.translate_seconds = out["qa"]["seconds"]
        man.save()
        q = out["qa"]
        print(f"  {'pass' if q['pass'] else 'CHECK'}  ratio {q['ratio']}  "
              f"paragraphs {q['english_paragraphs']}->{q['hindi_paragraphs']}  "
              f"skipped {len(q['skipped'])}  repairs {q['repairs'] or '-'}  "
              f"audit missing {len(q['audit_missing'])}  "
              f"english numbers {q['english_numbers'] or '-'}", flush=True)
    print(f"{g.calls} Gemini calls")


def cmd_lock(args) -> None:
    ep_root = E.root(args.episode)
    lock = load_lock(ep_root)
    for n in parse_range(args.chapters):
        f = ep_root / "hindi" / f"{n:05d}.txt"
        if not f.exists():
            sys.exit(f"{f} does not exist")
        lock[f.name] = E.sha256(f)
    E.lock_path(ep_root).write_text(json.dumps(lock, indent=1, sort_keys=True), encoding="utf-8")
    print(f"locked {len(lock)} chapters in {E.lock_path(ep_root)}")


DRAFT_HEADING = re.compile(r"^\s*\**\s*चैप्टर\s+[^\n:]+:[^\n]*$", re.M)


def cmd_draft(args) -> None:
    """Import a whole-episode script written in the Gemini app, one chapter per heading.

    The draft holds every chapter in order, each starting "चैप्टर <number>: <title>".
    Each chapter gets the same text checks as a Gemini API translation. A chapter
    that already has work is moved, with all of it, into old/v1/ first: text,
    scenes, images, audio and timing belong together.
    """
    ep_root = E.root(args.episode)
    man = Manifest.load(ep_root / "manifest.json")
    text = Path(args.file).read_text(encoding="utf-8").replace("\r\n", "\n")
    heads = list(DRAFT_HEADING.finditer(text))
    if len(heads) != len(man.chapters):
        sys.exit(f"{len(heads)} chapter headings in the draft, the episode has {len(man.chapters)}")
    want = set(parse_range(args.chapters))
    lock = load_lock(ep_root)
    glossary = load_glossary(ep_root / "glossary.json")
    for sub in ("hindi", "qa"):          # a fresh episode has neither folder yet
        (ep_root / sub).mkdir(exist_ok=True)
    for ch, m, nxt in zip(man.chapters, heads, heads[1:] + [None]):
        if ch.idx not in want:
            continue
        body = text[m.start(): nxt.start() if nxt else len(text)]
        paras = [" ".join(p.split()).strip("* ") for p in re.split(r"\n\s*\n", body) if p.strip()]
        problems = [f"[{k}] {paragraph_problem(p)}" for k, p in enumerate(paras) if paragraph_problem(p)]
        if problems:
            sys.exit(f"chapter {ch.idx}: {problems[:5]} - fix the draft first")
        name = f"{ch.idx:05d}"
        old = ep_root / "old" / "v1"
        for rel in (f"hindi/{name}.txt", f"scenes/{name}.json", f"scenes/{name}.brief.md",
                    f"qa/{name}.json", f"audio/{name}.mp3", f"timing/{name}.json",
                    f"images/c{ch.idx:04d}", f"video/c{ch.idx:04d}", f"video/c{ch.idx:04d}.mp4"):
            src = ep_root / rel
            if src.exists():
                (old / rel).parent.mkdir(parents=True, exist_ok=True)
                try:
                    src.replace(old / rel)
                except PermissionError:
                    # Open in a player: keep a copy. The stale original is replaced
                    # when the new narration is pulled.
                    if src.is_file():
                        shutil.copy2(src, old / rel)
                    print(f"  note: {rel} is open in another program - copied to old/, not moved")
        lock.pop(f"{name}.txt", None)
        hindi = "\n\n".join(paras) + "\n"
        (ep_root / "hindi" / f"{name}.txt").write_text(hindi, encoding="utf-8")
        english = " ".join(clean_paragraphs((ep_root / "chapters" / ch.source).read_text(encoding="utf-8")))
        (ep_root / "qa" / f"{name}.json").write_text(json.dumps({
            "chapter": ch.idx, "source": f"draft {Path(args.file).name}",
            "hindi_paragraphs": len(paras), "words": len(hindi.split()),
            "ratio": round(len(hindi) / len(english), 3),
            "english_numbers": english_numbers(hindi, glossary),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        lock[f"{name}.txt"] = E.sha256(ep_root / "hindi" / f"{name}.txt")
        ch.hindi, ch.status, ch.audio, ch.timing, ch.seconds = f"{name}.txt", TRANSLATED, None, None, None
        print(f"  chapter {ch.idx:>2}: {len(paras)} paragraphs, {len(hindi.split())} words")
    E.lock_path(ep_root).write_text(json.dumps(lock, indent=1, sort_keys=True), encoding="utf-8")
    man.save()


AUDIT_DRAFT = AUDIT.replace(
    "The retelling is deliberately about 20% shorter: dropped filler, repetition and description are fine.",
    "The retelling is deliberately MUCH shorter, a recap for a video: filler, repetition, descriptions of "
    "looks, clothes, places and characters, inner monologue and explanations of the world are all cut on "
    "purpose and are fine.")


def cmd_audit(args) -> None:
    """Ask Gemini which plot points an imported draft lost, chapter by chapter."""
    ep_root = E.root(args.episode)
    man = Manifest.load(ep_root / "manifest.json")
    g = Gemini(args.model, ep_root / "qa" / "audit.log.jsonl")
    want = set(parse_range(args.chapters))
    total = 0
    for ch in man.chapters:
        if ch.idx not in want:
            continue
        name = f"{ch.idx:05d}"
        paras = clean_paragraphs((ep_root / "chapters" / ch.source).read_text(encoding="utf-8"))
        hindi = (ep_root / "hindi" / f"{name}.txt").read_text(encoding="utf-8")
        raw = g.ask(AUDIT_DRAFT, "ENGLISH:\n" + "\n\n".join(f"[{i}] {p}" for i, p in enumerate(paras, 1))
                    + "\n\nHINGLISH:\n" + hindi, json_out=True, temperature=0.0)
        try:
            missing = _extract_json(raw).get("missing", [])
        except Exception:
            missing = [{"point": "audit reply was not JSON", "english_para": None}]
        (ep_root / "qa" / f"{name}.audit.json").write_text(
            json.dumps({"chapter": ch.idx, "model": g.model, "missing": missing},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        total += len(missing)
        print(f"chapter {ch.idx}: {len(missing)} plot points missing")
        for m in missing:
            print(f"    [{m.get('english_para')}] {m.get('point')}")
    print(f"total missing: {total}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--episode", type=int, required=True)
    r.add_argument("--chapters", required=True, help="e.g. 1 or 2-20")
    r.add_argument("--model", default=MODEL)
    r.add_argument("--dry-run", action="store_true", help="print the prompt, send nothing")
    r.set_defaults(fn=cmd_run)
    k = sub.add_parser("lock")
    k.add_argument("--episode", type=int, required=True)
    k.add_argument("--chapters", required=True)
    k.set_defaults(fn=cmd_lock)
    d = sub.add_parser("draft", help="import a whole-episode script from the Gemini app, and lock it")
    d.add_argument("--episode", type=int, required=True)
    d.add_argument("--file", required=True)
    d.add_argument("--chapters", default="1-999", help="which chapters to import, e.g. 1-2")
    d.set_defaults(fn=cmd_draft)
    a = sub.add_parser("audit", help="list plot points an imported draft lost (Gemini)")
    a.add_argument("--episode", type=int, required=True)
    a.add_argument("--chapters", default="1-999")
    a.add_argument("--model", default=MODEL)
    a.set_defaults(fn=cmd_audit)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
