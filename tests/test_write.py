"""novel write: the Gemini API path (fixture book 'tide', invented text, temp NOVEL_HOME, a local fake Gemini server).

No network: NOVEL_GEMINI_URL points the client at a local HTTP server that returns scripted replies per request.
Time is virtual: the injected sleep advances an injected clock, so pacing and waits cost nothing.
"""

import json
import re
import hashlib
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel import bible as B
from novel.gemini import contract as C
from novel.gemini import write as W
from novel.stages import episode_scenes as S
from test_import import (BELL_TOWER, NAME_LINE, block, chapter_lines, counts, ep, image, listing, run as import_run,
                         setup_book)

KEY1, KEY2 = "AIzaFAKEKEYONE-0000000000000001", "AIzaFAKEKEYTWO-0000000000000002"
MAIN, FALLBACK = "gemini-3.5-flash-lite", "fallback-model"

# chapter 3: no Hindi yet (invented)
EN3 = ("Chapter 3: The Lamp\n\nMaren carried the old lamp down to the quay. Tobias was waiting by the boats and "
       "asked her why she had brought it.\n\nShe told him that the harbour needed a light before the fog came in, "
       "and that nobody else would climb the tower tonight.\n\nTobias laughed, then stopped, because the bell had "
       "begun to ring on its own.")
HI3 = [
    "चैप्टर तीन: दीपक",
    "मैरेन पुराना दीपक उठाकर घाट की तरफ उतरती है। टोबायस नावों के पास खड़ा उसका इंतज़ार कर रहा है और पूछता है कि "
    "वह इसे यहाँ क्यों लाई है।",
    "वह बताती है कि कोहरा आने से पहले बंदरगाह को एक रोशनी चाहिए। और आज रात कोई दूसरा घंटाघर की सीढ़ियाँ नहीं चढ़ेगा।",
    "टोबायस हँसता है, फिर अचानक चुप हो जाता है। वजह यह है कि घंटी अपने आप बजने लगी है।",
    "दोनों एक-दूसरे को देखते हैं। मैरेन दीपक को कसकर पकड़ लेती है और टोबायस धीरे से कहता है कि अब पीछे हटने का "
    "कोई रास्ता नहीं बचा।",
]
FIX_WORDS = sum(len(p.split()) for p in HI3[1:])
HUT = {"type": "location", "key": "signal_hut", "name": "Signal Hut", "kind": "interior", "chapters": "1-3",
       "block": ("A cramped wooden hut at the end of the breakwater. A brass signal lamp on a swivel stand faces "
                 "the sea window, a cot with a grey blanket stands against the back wall, charts are pinned to "
                 "every plank and a small iron stove ticks in the corner."),
       "default_light": "warm lamp light"}


def hut_loc(first, last):
    return {**HUT, "chapters": f"{first}-{last}"}


# ---------------------------------------------------------------- the fake Gemini server

def recap_of(system):
    """A valid recap for the band the rules name: the middle of the band, one paragraph."""
    low, high = (int(x) for x in re.search(r"(\d+) to (\d+) words in all", system).groups())
    return " ".join(["मैरेन"] * ((low + high) // 2))


def ok(text, finish="STOP"):
    return ("text", text, finish)


def status(code):
    return ("status", code)


class Fake:
    """Scripted Gemini endpoint. `rules` are (predicate, deque of responses): the first rule whose predicate matches
    a request supplies (and spends) its next response; with none left, the default clean reply is used."""

    def __init__(self):
        self.requests = []
        self.rules = []
        self.limited_keys = set()            # keys that always get HTTP 429
        self.limited_models = set()          # models for which every key gets HTTP 429
        self.after = None
        self.lock = threading.Lock()
        self.batch_first = {}

    def on(self, pred, *responses):
        self.rules.append((pred, deque(responses)))

    # -- classification of a request
    @staticmethod
    def classify(system, user):
        r = SimpleNamespace(kind="?", n=None, batch=None, retry="YOUR PREVIOUS ANSWER" in user)
        if "HOW YOU ANSWER" in system:
            r.kind = "hindi"
            r.n = int(re.search(r"CHAPTER (\d+) \(English\)", user).group(1))
            r.cont = "YOUR ANSWER SO FAR" in user
        elif "THE RECAP" in system:
            r.kind = "recap"
        elif "Return the BIBLE block" in user:
            r.kind = "bible"
            r.batch = re.search(r"BEGIN BIBLE (B\d+-\d+) ->", user).group(1)
        elif "the second check found problems" in user:
            r.kind = "fix"
            r.n = int(re.search(r"Chapter (\d+) \(c\d+\)", user).group(1))
        elif "WINDOWS OF CHAPTER" in user:
            m = re.search(r"##### CHAPTER (\d+): (\d+) WINDOWS", user)
            r.kind, r.n, r.count = "images", int(m.group(1)), int(m.group(2))
            m = re.search(r"BEGIN BIBLE (B\d+-\d+)-c", user)
            r.batch = m.group(1) if m else None
            r.rest = "stopped early" in user
        return r

    def default(self, r):
        if r.kind == "hindi":
            if r.cont:
                return ok("\n\n".join(HI3[3:]))
            return ok("\n\n".join(HI3))
        if r.kind == "recap":
            return ok(recap_of(r.system))
        if r.kind == "bible":
            a, z = (int(x) for x in re.fullmatch(r"B(\d+)-(\d+)", r.batch).groups())
            return ok("Here is the bible.\n\n" + block("BIBLE", r.batch, [
                {**BELL_TOWER, "chapters": f"{a}-{z}"}, NAME_LINE]))
        if r.kind == "images":
            lo, hi = 1, r.count
            if r.rest:
                miss = re.search(r"missing windows (\d+)(?:-(\d+))?", r.user)
                lo = int(miss.group(1))
                hi = int(miss.group(2) or miss.group(1))
            return ok(block("IMAGES", f"c{r.n:04d}", chapter_lines(r.count, r.n, start=lo, end=hi)))
        if r.kind == "fix":
            m = re.search(r"(===== BEGIN PATCH.*?===== END PATCH c\d+ =====)", r.user, re.S)
            return ok(m.group(1).replace('"var":"storm_coat"', '"var":"base"'))
        return ok("")

    # -- the server
    def respond(self, r):
        with self.lock:
            self.requests.append(r)
            if r.key in self.limited_keys or r.model in self.limited_models:
                return status(429)
            for pred, queue in self.rules:
                if queue and pred(r):
                    res = queue.popleft()
                    return res(r) if callable(res) else res
            return self.default(r)

    def count(self, kind=None, n=None):
        return len([r for r in self.requests if (kind is None or r.kind == kind) and (n is None or r.n == n)])


@pytest.fixture
def fake(monkeypatch):
    f = Fake()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            system = body["system_instruction"]["parts"][0]["text"]
            user = body["contents"][0]["parts"][0]["text"]
            r = f.classify(system, user)
            r.model, r.key = self.path.strip("/"), self.headers["x-goog-api-key"]
            r.system, r.user = system, user
            res = f.respond(r)
            if f.after:
                f.after(r)
            if res[0] == "status":
                self.send_response(res[1])
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"code": res[1], "status": "RESOURCE_EXHAUSTED"}}).encode())
                return
            _, text, finish = res
            out = {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}],
                   "usageMetadata": {"promptTokenCount": len(user) // 4, "candidatesTokenCount": len(text) // 4,
                                     "totalTokenCount": (len(user) + len(text)) // 4}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NOVEL_GEMINI_URL", f"http://127.0.0.1:{srv.server_port}/{{model}}")
    yield f
    srv.shutdown()


class VClock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = VClock()
    monkeypatch.setattr(W, "SLEEP", c.sleep)
    monkeypatch.setattr(W, "CLOCK", c.now)
    return c


# ---------------------------------------------------------------- the book

def make_book(tmp_path, monkeypatch, settings=None, chapter3=False, keys=(KEY1, KEY2)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    # the invented fixture Hindi is about FIX_WORDS words: the word band (translate.target_words) is set to that size
    # for these fixture books; the real default (800 words) is used by the word tests with words_book
    settings = {"translate": {"target_words": FIX_WORDS, "words_tolerance": 0.2}, **(settings or {})}
    bd = setup_book(tmp_path, monkeypatch, settings=settings)
    home = tmp_path / "home"
    (home / "secrets.env").write_text("".join(f"GEMINI_API_KEY{'' if i == 0 else i + 1}={k}\n"
                                                for i, k in enumerate(keys)), encoding="utf-8")
    if chapter3:
        (bd / "chapters" / "0003_the-lamp.txt").write_text(EN3, encoding="utf-8")
        (ep(bd) / "episode.json").write_text(json.dumps({"episode": 1, "start": 1, "end": 3}), encoding="utf-8")
    return bd


@pytest.fixture
def book(tmp_path, monkeypatch, clock):
    return make_book(tmp_path, monkeypatch)


@pytest.fixture
def book3(tmp_path, monkeypatch, clock):
    return make_book(tmp_path, monkeypatch, chapter3=True)


def write(capsys, *args, book="tide"):
    from novel.cli import main
    code = main(["write", "--book", book, "--ep", "1", *args])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def scene(bd, n):
    return json.loads(S.scenes_path(ep(bd), n).read_text(encoding="utf-8"))


def progress(bd, bid="B001-002"):
    return json.loads((bd / "gemini" / bid / "progress.json").read_text(encoding="utf-8"))


def norm(doc):
    doc = json.loads(json.dumps(doc))
    doc["source"] = {"batch": doc["source"]["batch"]}
    return doc


def tree_text(root: Path, skip=()):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in skip:
            yield p, p.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- a clean run

def test_clean_run_writes_both_chapters_and_matches_what_novel_import_makes(tmp_path, monkeypatch, capsys, fake, clock):
    bd = make_book(tmp_path / "one", monkeypatch)
    code, out = write(capsys)
    assert code == 0, out
    assert fake.count("hindi") == 0                       # both chapters already have locked Hindi
    assert fake.count("bible") == 1 and fake.count("images") == 2
    c = counts(bd)
    for n in (1, 2):
        assert len(scene(bd, n)["scenes"]) == c[n] and scene(bd, n)["version"] == 2
        assert scene(bd, n)["source"]["via"] == "api" and scene(bd, n)["source"]["model"] == MAIN
        assert S.check_chapter(ep(bd), n) == []
    assert f"chapter 1: scenes: call 1 ... imported {c[1]} of {c[1]} (" in out
    assert "novel qa --chapters 1-2" in out and "Run log: " in out
    assert re.search(r"Gemini calls \(requests sent, retries included\): 3; tokens in [\d,]+, out [\d,]+", out)
    assert "rate-limit switches: 0" in out
    # the progress file records the model and the attempts per block
    prog = progress(bd)
    assert prog["bible"]["done"] and prog["bible"]["blocks"]["B001-002"]["model"] == MAIN
    assert prog["chapters"]["1"]["api"]["models"] == [MAIN] and prog["chapters"]["1"]["api"]["attempts"] == 1
    # the run log and the saved requests and replies sit next to the raw archive
    logs = list((bd / "gemini" / "B001-002").glob("run-*.log"))
    assert len(logs) == 1 and "request 1" in logs[0].read_text(encoding="utf-8")
    api = bd / "gemini" / "B001-002" / "api"
    assert len(list(api.glob("*.request.txt"))) == 3 and len(list(api.glob("*.reply.txt"))) == 3
    assert len(list((bd / "gemini" / "B001-002" / "raw").glob("*.txt"))) == 3
    # the scene files equal what `novel import` makes from the same replies
    replies = [p.read_text(encoding="utf-8") for p in sorted(api.glob("*.reply.txt"))]
    mine = {n: norm(scene(bd, n)) for n in (1, 2)}
    (tmp_path / "two").mkdir()
    bd2 = setup_book(tmp_path / "two", monkeypatch)
    for text in replies:
        assert import_run(monkeypatch, capsys, text)[0] == 0
    assert {n: norm(scene(bd2, n)) for n in (1, 2)} == mine


def test_the_second_chapters_request_knows_a_location_the_first_chapters_reply_added(book, monkeypatch, capsys, fake):
    fake.on(lambda r: r.kind == "images" and r.n == 1 and not r.retry, ok(
        block("BIBLE", "B001-002-c0001", [hut_loc(1, 2)])
        + block("IMAGES", "c0001", [dict(l, loc="signal_hut") if l["type"] == "group" and l["group"] == "g01" else l
                                    for l in chapter_lines(counts(book)[1], 1)])))
    code, out = write(capsys)
    assert code == 0, out
    first, second = [r for r in fake.requests if r.kind == "images"]
    assert "key: signal_hut" not in first.user and "key: signal_hut" in second.user
    assert "signal_hut" in B.load_locations(book / "bible")["locations"]
    assert "bible lines for this chapter imported" in out
    assert "where" in first.system and '"where":"indoor|outdoor"' in first.system


def test_requests_carry_the_rules_the_bible_one_chapter_and_its_windows_only(book, capsys, fake):
    write(capsys)
    r1, r2 = [r for r in fake.requests if r.kind == "images"]
    assert "CHAPTER 1 (English)" in r1.user and "CHAPTER 2 (English)" not in r1.user
    assert "##### CHAPTER 1:" in r1.user and "##### CHAPTER 2:" not in r1.user
    assert "CURRENT BIBLE" in r1.user and "NAMES (English = Devanagari" in r1.user
    assert "TO REVIEW" not in r1.user
    assert "HOW WE WORK" not in r1.system and "type next" not in r1.system.lower() and "I write" not in r1.system
    assert "Write exactly one image line per window" in r1.system and "PART B - THE PICTURES" in r1.system
    bible = next(r for r in fake.requests if r.kind == "bible")
    assert "CHAPTER 1 (English)" not in bible.user and "CHAPTERS 1-2 (English)" in bible.user
    assert "PART C - THE BIBLE" in bible.system and "PART B" not in bible.system


# ---------------------------------------------------------------- rate limits

def test_rate_limit_on_key_one_moves_to_key_two(book, capsys, fake):
    fake.limited_keys = {KEY1}
    code, out = write(capsys)
    assert code == 0, out
    keys = [r.key for r in fake.requests]
    assert keys[0] == KEY1 and set(keys[1:]) == {KEY2}               # one 429, then key 2 for every later call
    assert "rate limit: key 1 of 2" in out and "switching to key 2" in out
    assert "rate-limit switches: 1" in out
    assert KEY1 not in out and KEY2 not in out


def test_every_key_limited_waits_then_goes_on(book, capsys, fake, clock):
    n = []
    fake.on(lambda r: True, status(429), status(429))
    code, out = write(capsys)
    assert code == 0, out
    assert 65 in clock.sleeps
    assert "all 2 keys rate-limited, waiting 65s" in out and "rate-limit switches: 2" in out


def test_quota_gone_stops_cleanly_and_a_rerun_continues(book, capsys, fake, clock):
    fake.limited_keys = {KEY1, KEY2}
    code, out = write(capsys)
    assert code == 1
    assert "STOPPED: the Gemini quota is used up" in out and "Run the same command again later" in out
    assert clock.sleeps.count(65) == 4                              # bounded: it does not loop
    assert len(fake.requests) == 2 * 5                              # five sweeps of two keys
    assert not S.scenes_path(ep(book), 1).exists()
    fake.limited_keys = set()
    code, out = write(capsys)
    assert code == 0, out
    assert all(S.scenes_path(ep(book), n).exists() for n in (1, 2))


def test_fallback_model_is_tried_when_every_key_is_limited_and_recorded(tmp_path, monkeypatch, capsys, fake, clock):
    bd = make_book(tmp_path, monkeypatch, settings={"gemini": {"fallback_models": [FALLBACK]}})
    fake.limited_models = {MAIN}
    code, out = write(capsys)
    assert code == 0, out
    assert f"every key is limited on {MAIN}; trying the fallback model {FALLBACK}" in out
    assert 65 not in clock.sleeps                                    # no waiting: the fallback answered
    assert scene(bd, 1)["source"]["model"] == FALLBACK
    assert progress(bd)["chapters"]["1"]["api"]["models"] == [FALLBACK]
    assert progress(bd)["bible"]["blocks"]["B001-002"]["model"] == FALLBACK
    assert f"Replies by model: {FALLBACK}" in out


def test_pacing_uses_the_injected_sleep(book, capsys, fake, clock):
    code, out = write(capsys)
    assert code == 0
    assert clock.sleeps == [4.0, 4.0]                               # three calls, four seconds apart


def test_pacing_is_a_setting(tmp_path, monkeypatch, capsys, fake, clock):
    make_book(tmp_path, monkeypatch, settings={"gemini": {"min_seconds_between_calls": 0}})
    write(capsys)
    assert clock.sleeps == []


# ---------------------------------------------------------------- rejected, partial, failing blocks

def bad_image_two(count):
    lines = chapter_lines(count, 1)
    return [dict(l, action="too short") if l["type"] == "image" and l["n"] == 2 else l for l in lines]


def test_a_rejected_block_is_asked_again_with_the_fix_list_and_the_rejected_block(book, capsys, fake):
    fake.on(lambda r: r.kind == "images" and r.n == 1, ok(block("IMAGES", "c0001", bad_image_two(counts(book)[1]))))
    code, out = write(capsys)
    assert code == 0, out
    assert fake.count("images", 1) == 2 and fake.count("images", 2) == 1
    retry = [r for r in fake.requests if r.kind == "images" and r.n == 1][1]
    assert "FIX:" in retry.user and "action: " in retry.user and "needs 60-600" in retry.user
    assert "YOUR PREVIOUS ANSWER (rejected)" in retry.user and '"action": "too short"' in retry.user
    assert "repeat c0001 IMAGES" in retry.user
    assert "chapter 1: scenes: rejected" in out
    assert scene(book, 1)["source"]["via"] == "api" and progress(book)["chapters"]["1"]["api"]["attempts"] == 2


def test_a_partial_block_gets_a_request_for_the_missing_windows_only(book, capsys, fake):
    count = counts(book)[1]
    half = count // 2 if count // 2 >= 1 else 1
    part = block("IMAGES", "c0001", chapter_lines(count, 1, start=1, end=half)) + ">>> MORE c0001 IMAGES\n"
    fake.on(lambda r: r.kind == "images" and r.n == 1, ok(part))
    code, out = write(capsys)
    assert code == 0, out
    rest = [r for r in fake.requests if r.kind == "images" and r.n == 1][1]
    assert "stopped early" in rest.user and f"missing windows {half + 1}" in rest.user
    assert "Do not repeat earlier lines" in rest.user
    assert f"asking for windows {half + 1}" in out
    assert len(scene(book, 1)["scenes"]) == count and S.check_chapter(ep(book), 1) == []


def test_a_short_block_without_more_is_filed_as_a_part_and_the_rest_is_asked_for(book, capsys, fake):
    count = counts(book)[1]
    short = block("IMAGES", "c0001", chapter_lines(count, 1, start=1, end=count - 1))
    fake.on(lambda r: r.kind == "images" and r.n == 1, ok(short))
    code, out = write(capsys)
    assert code == 0, out
    rest = [r for r in fake.requests if r.kind == "images" and r.n == 1][1]
    assert f"missing windows {count}" in rest.user and "stopped early" in rest.user
    assert len(scene(book, 1)["scenes"]) == count


def test_a_reply_cut_by_the_output_limit_keeps_its_complete_lines(book, capsys, fake):
    count = counts(book)[1]
    text = block("IMAGES", "c0001", chapter_lines(count, 1, start=1, end=count - 2), end=False)
    cut = text[:text.rindex("}") - 20]                           # the last line is cut in the middle
    fake.on(lambda r: r.kind == "images" and r.n == 1, ok(cut, finish="MAX_TOKENS"))
    code, out = write(capsys)
    assert code == 0, out
    assert "cut off by the output limit; keeping its complete lines" in out
    assert len(scene(book, 1)["scenes"]) == count


def test_a_block_that_fails_every_attempt_is_left_reported_and_the_run_goes_on(book, capsys, fake):
    fake.on(lambda r: r.kind == "images" and r.n == 1, *[ok(block("IMAGES", "c0001", bad_image_two(counts(book)[1])))] * 9)
    code, out = write(capsys)
    assert code == 1, out
    assert fake.count("images", 1) == 3                              # gemini.max_attempts_per_block
    assert not S.scenes_path(ep(book), 1).exists()
    assert len(scene(book, 2)["scenes"]) == counts(book)[2]          # the next chapter still ran
    assert "not complete after 3 attempts" in out and "problem: the IMAGES block was still not accepted" in out
    assert re.search(r"^1\s+existed\s+ok\s+-\s+3\s+.*failed", out, re.M)
    assert re.search(r"^2\s+existed\s+ok\s+\d+ of \d+\s+1\s+.*complete", out, re.M)


def test_a_reply_with_a_block_that_was_not_asked_for_is_never_imported(book, capsys, fake):
    wrong = block("IMAGES", "c0002", chapter_lines(counts(book)[2], 2))
    fake.on(lambda r: r.kind == "images" and r.n == 1, ok(wrong))
    code, out = write(capsys)
    assert code == 0, out
    assert "not asked for" in out
    assert scene(book, 1)["chapter"] == 1


def test_max_attempts_is_a_setting(tmp_path, monkeypatch, capsys, fake, clock):
    bd = make_book(tmp_path, monkeypatch, settings={"gemini": {"max_attempts_per_block": 1}})
    fake.on(lambda r: r.kind == "images" and r.n == 1, *[ok(block("IMAGES", "c0001", bad_image_two(counts(bd)[1])))] * 3)
    code, _ = write(capsys)
    assert code == 1 and fake.count("images", 1) == 1


# ---------------------------------------------------------------- translation

def test_chapters_with_hindi_are_never_translated_and_a_new_chapter_is_written_checked_and_locked(book3, capsys, fake):
    code, out = write(capsys, "--chapters", "1-3")
    assert code == 0, out
    assert fake.count("hindi") == 1 and {r.n for r in fake.requests if r.kind == "hindi"} == {3}
    hf = ep(book3) / "hindi" / "00003.txt"
    assert hf.read_text(encoding="utf-8") == "\n\n".join(HI3) + "\n"
    lock = json.loads((ep(book3) / "hindi.lock.json").read_text(encoding="utf-8"))
    assert "00003.txt" in lock and set(lock) >= {"00001.txt", "00002.txt"}
    from novel.gemini import prompt as P
    assert P.hindi_state(ep(book3), 3) == "locked"
    qa = json.loads((ep(book3) / "qa" / "00003.json").read_text(encoding="utf-8"))
    assert qa["source"].startswith("api ") and qa["hindi_paragraphs"] == len(HI3)
    assert len(scene(book3, 3)["scenes"]) == len(S.chapter_windows(S.chapter_paras(ep(book3), 3), S.PACING))
    # chapters 1 and 2 keep their Hindi byte for byte
    for n in (1, 2):
        assert (ep(book3) / "hindi" / f"{n:05d}.txt").read_text(encoding="utf-8") == \
            (Path(__file__).parent / "fixtures" / "gemini" / "hindi" / f"{n:05d}.txt").read_text(encoding="utf-8")
    assert re.search(r"^3\s+written\s+ok", out, re.M) and re.search(r"^1\s+existed\s+ok", out, re.M)
    tr = next(r for r in fake.requests if r.kind == "hindi")
    assert "THE NARRATION" in tr.system and "{1}" not in tr.system and "PICTURE MARKERS" not in tr.system
    assert "BIBLE" not in tr.system and "NAMES (English = Devanagari" in tr.user and "Maren = मैरेन" in tr.user


def test_a_translation_cut_off_by_the_output_limit_is_continued_and_joined(book3, capsys, fake):
    cut = "\n\n".join(HI3[:3]) + "\n\n" + HI3[3][:30]
    fake.on(lambda r: r.kind == "hindi" and not r.cont, ok(cut, finish="MAX_TOKENS"))
    code, out = write(capsys, "--chapters", "3")
    assert code == 0, out
    reqs = [r for r in fake.requests if r.kind == "hindi"]
    assert len(reqs) == 2 and reqs[1].cont
    assert HI3[2] in reqs[1].user and HI3[3][:30] not in reqs[1].user      # the cut paragraph is not shown as done
    assert "cut off by the output limit after 3 full paragraphs" in out
    assert (ep(book3) / "hindi" / "00003.txt").read_text(encoding="utf-8") == "\n\n".join(HI3) + "\n"


def test_a_bad_translation_is_asked_again_with_the_problems_listed(book3, capsys, fake):
    bad = HI3[:2] + ["वह 3 दिन बाद लौटी।"] + HI3[3:]                      # digits
    fake.on(lambda r: r.kind == "hindi", ok("\n\n".join(bad)))
    code, out = write(capsys, "--chapters", "3")
    assert code == 0, out
    retry = [r for r in fake.requests if r.kind == "hindi"][1]
    assert "found these problems" in retry.user and "paragraph 3 contains digits" in retry.user
    assert "YOUR PREVIOUS ANSWER" in retry.user
    assert "3 दिन" not in (ep(book3) / "hindi" / "00003.txt").read_text(encoding="utf-8")


def test_a_missing_name_is_a_problem_but_the_last_attempt_is_kept_with_a_warning(book3, capsys, fake):
    nameless = [p.replace("मैरेन", "वह") for p in HI3]
    fake.on(lambda r: r.kind == "hindi", *[ok("\n\n".join(nameless))] * 4)
    code, out = write(capsys, "--chapters", "3")
    assert code == 0, out
    assert fake.count("hindi") == 4
    assert (ep(book3) / "hindi" / "00003.txt").exists()
    assert "accepted with a name problem" in json.dumps(
        json.loads((ep(book3) / "qa" / "00003.json").read_text(encoding="utf-8")), ensure_ascii=False)


def test_a_translation_that_never_passes_is_not_stored_and_the_scenes_wait(book3, capsys, fake):
    fake.on(lambda r: r.kind == "hindi", *[ok("not Hindi at all")] * 4)
    code, out = write(capsys, "--chapters", "3")
    assert code == 1
    assert not (ep(book3) / "hindi" / "00003.txt").exists()
    assert fake.count("bible") == 0 and fake.count("images") == 0
    assert re.search(r"^3\s+failed", out, re.M)


def test_no_translate_refuses_a_chapter_without_hindi(book3, capsys, fake):
    code, out = write(capsys, "--chapters", "1-3", "--no-translate")
    assert code == 1
    assert fake.count("hindi") == 0
    assert "refused (--no-translate)" in out and not (ep(book3) / "hindi" / "00003.txt").exists()
    assert S.scenes_path(ep(book3), 1).exists()                      # the chapters with Hindi were still done


# ---------------------------------------------------------------- re-runs, dry run, redo, limits

def test_a_rerun_skips_finished_chapters_and_makes_no_call(book, capsys, fake):
    assert write(capsys)[0] == 0
    n = len(fake.requests)
    code, out = write(capsys)
    assert code == 0 and len(fake.requests) == n
    assert "Nothing to do" in out and "already complete" in out


def test_a_rerun_after_a_failure_reuses_the_batch_and_the_bible_and_only_redoes_the_failed_chapter(book, capsys, fake):
    fake.on(lambda r: r.kind == "images" and r.n == 1, *[ok(block("IMAGES", "c0001", bad_image_two(counts(book)[1])))] * 3)
    assert write(capsys)[0] == 1
    before = len(fake.requests)
    code, out = write(capsys)
    assert code == 0, out
    new = fake.requests[before:]
    assert [r.kind for r in new] == ["images"] and new[0].n == 1       # no bible call, no call for chapter 2
    assert "bible: already imported in an earlier run, skipped" in out and "reusing the batch" in out


def test_dry_run_sends_nothing_and_writes_nothing(book3, capsys, fake):
    before = listing(book3)
    code, out = write(capsys, "--chapters", "1-3", "--dry-run")
    assert code == 0, out
    assert fake.requests == [] and listing(book3) == before
    assert "chapter 3: hindi: 1 call, request" in out and "chapter 1: scenes: 1 call, request" in out
    assert "bible: 1 call, request about" in out and "Total of the first requests" in out


def test_dry_run_needs_no_keys(tmp_path, monkeypatch, capsys, fake, clock):
    make_book(tmp_path, monkeypatch, keys=())
    code, out = write(capsys, "--dry-run")
    assert code == 0 and fake.requests == []


def test_redo_writes_the_chapter_again_and_keeps_the_old_files(book, capsys, fake):
    assert write(capsys)[0] == 0
    old = scene(book, 1)
    n = len(fake.requests)
    again = [dict(l, action=l["action"].replace("Picture", "Scene")) if l["type"] == "image" else l
             for l in chapter_lines(counts(book)[1], 1)]
    fake.on(lambda r: r.kind == "images" and r.n == 1, ok(block("IMAGES", "c0001", again)))
    code, out = write(capsys, "--redo", "1")
    assert code == 0, out
    new = fake.requests[n:]
    assert [r.kind for r in new] == ["images"]                         # one call; no bible, no other chapter
    moved = ep(book) / "old" / "v1" / "scenes" / "00001.json"
    assert moved.exists() and json.loads(moved.read_text(encoding="utf-8"))["scenes"] == old["scenes"]
    assert scene(book, 1)["scenes"][0]["action"].startswith("Scene 1")
    assert "old files of chapter 1 moved" in "".join(p.read_text(encoding="utf-8") for p in
                                                      (book / "gemini").rglob("run-*.log"))


def test_chapters_per_run_limits_the_run_and_the_next_run_takes_the_rest(tmp_path, monkeypatch, capsys, fake, clock):
    bd = make_book(tmp_path, monkeypatch)
    code, out = write(capsys, "--max-chapters", "1")
    assert code == 0, out
    assert S.scenes_path(ep(bd), 1).exists() and not S.scenes_path(ep(bd), 2).exists()
    assert "chapter 2: left out: over the limit" in out
    code, out = write(capsys, "--max-chapters", "1")
    assert code == 0 and S.scenes_path(ep(bd), 2).exists()


def test_parallel_two_chapters_with_a_location_added_by_both_replies(tmp_path, monkeypatch, capsys, fake, clock):
    bd = make_book(tmp_path, monkeypatch, chapter3=True)
    # chapter 3 needs its Hindi first: give it, so all three chapters can run side by side
    (ep(bd) / "hindi" / "00003.txt").write_text("\n\n".join(HI3) + "\n", encoding="utf-8")
    from types import SimpleNamespace as NS
    from novel.stages import episode_translate as T
    T.cmd_lock(NS(episode=1, chapters="3"))

    def hut(r):
        n = r.n
        count = r.count
        lines = [dict(l, loc="signal_hut") if l["type"] == "group" and l["group"] == "g01" else l
                 for l in chapter_lines(count, n)]
        return ok(block("BIBLE", f"B001-003-c{n:04d}", [hut_loc(1, 3)]) + block("IMAGES", f"c{n:04d}", lines))

    for n in (1, 2, 3):
        fake.on(lambda r, n=n: r.kind == "images" and r.n == n and not r.retry, hut)
    code, out = write(capsys, "--parallel", "2")
    assert code == 0, out
    assert all(S.scenes_path(ep(bd), n).exists() for n in (1, 2, 3))
    assert "signal_hut" in B.load_locations(bd / "bible")["locations"]
    logs = "".join(p.read_text(encoding="utf-8") for p in (bd / "gemini").rglob("run-*.log"))
    assert logs.count("key 'signal_hut' already exists") >= 2         # the second and third BIBLE blocks lost the race
    assert all(scene(bd, n)["groups"]["g01"]["loc"] == "signal_hut" for n in (1, 2, 3))


# ---------------------------------------------------------------- Ctrl+C

def test_stop_after_the_current_call_leaves_a_consistent_state_and_a_rerun_finishes(book, capsys, fake):
    from novel.gemini import write as W2
    args = SimpleNamespace(book="tide", ep=1, chapters=None, max_chapters=None, model=None, parallel=None,
                           dry_run=False, no_translate=False, redo=None, fix=None)
    runner = W2.Runner("tide", 1, args)
    fake.after = lambda r: runner.stop.set() if r.kind == "images" and r.n == 1 else None
    code = runner.run()
    out = capsys.readouterr().out
    assert code == 1
    assert S.scenes_path(ep(book), 1).exists() and not S.scenes_path(ep(book), 2).exists()
    assert fake.count("images", 2) == 0
    assert re.search(r"^2\s+existed\s+ok\s+-\s+0\s+-\s+stopped", out, re.M)
    fake.after = None
    code, out = write(capsys)
    assert code == 0 and S.scenes_path(ep(book), 2).exists()


# ---------------------------------------------------------------- no key anywhere

def test_no_api_key_is_in_the_console_the_log_the_saved_requests_or_the_data(tmp_path, monkeypatch, capsys, fake, clock):
    bd = make_book(tmp_path, monkeypatch)
    fake.limited_keys = {KEY1}
    code, out = write(capsys)
    assert code == 0
    assert KEY1 not in out and KEY2 not in out and "FAKEKEY" not in out
    for p, text in tree_text(tmp_path, skip={"secrets.env"}):
        assert "FAKEKEY" not in text, p
    assert any("rate limit: key 1 of 2" in t for _, t in tree_text(bd / "gemini") if "run-" in _.name)


# ---------------------------------------------------------------- QA fix by the API

@pytest.fixture
def qa_ready(book, capsys, fake, monkeypatch):
    assert write(capsys)[0] == 0
    from test_qa import edit_scenes
    edit_scenes(book, 1, lambda d: d["scenes"][2]["cast"][0].update({"var": "storm_coat"}))
    from novel.cli import main
    assert main(["qa", "--book", "tide", "--ep", "1", "--only-code", "--chapters", "1"]) == 1
    capsys.readouterr()
    return book


def test_fix_sends_the_qa_message_and_imports_the_patch(qa_ready, capsys, fake):
    n = len(fake.requests)
    code, out = write(capsys, "--fix", "1")
    assert code == 0, out
    new = fake.requests[n:]
    assert len(new) == 1 and new[0].kind == "fix"
    assert "BEGIN PATCH c0001" in new[0].user and "[image 3]" in new[0].user and "storm_coat" in new[0].user
    assert "CHAPTER 1 (English)" in new[0].user and "WINDOWS OF CHAPTER 1" in new[0].user
    assert "imported" in out and "Next: novel qa --chapters 1" in out
    assert scene(qa_ready, 1)["scenes"][2]["cast"][0]["var"] == "base"
    from novel.cli import main
    assert main(["qa", "--book", "tide", "--ep", "1", "--only-code", "--chapters", "1"]) == 0


def test_fix_is_bounded_and_changes_nothing_when_the_patch_never_passes(qa_ready, capsys, fake):
    n = len(fake.requests)
    fake.on(lambda r: r.kind == "fix", *[ok("no block here")] * 5)
    code, out = write(capsys, "--fix", "1")
    assert code == 1 and len(fake.requests) - n == 3
    assert "Nothing was changed" in out
    assert scene(qa_ready, 1)["scenes"][2]["cast"][0]["var"] == "storm_coat"


def test_fix_without_a_qa_result_says_what_to_run(book, capsys, fake):
    assert write(capsys)[0] == 0
    code, out = write(capsys, "--fix", "1")
    assert code == 1 and "novel qa --chapters 1" in out


# ---------------------------------------------------------------- status and the rest

def test_status_names_novel_write_first_and_the_hand_paste_path_as_the_alternative(tmp_path, monkeypatch, capsys, clock):
    bd = make_book(tmp_path, monkeypatch)
    from novel.cli import main
    main(["status", "--book", "tide", "--ep", "1"])
    out = capsys.readouterr().out
    rows = out.splitlines()
    k = next(i for i, l in enumerate(rows) if "no scenes yet" in l)
    line = " ".join(l.strip() for l in [rows[k]] + [x for x in rows[k + 1:k + 3] if x.startswith("     ")])
    assert "-> novel write" in line and "or by hand: novel prompt --chapters 1-2" in line
    assert line.index("novel write") < line.index("novel prompt") and "novel import" in line


def test_the_write_command_is_listed_and_documented():
    from novel import cli
    assert "write" in [n for n, _h, _s in cli.COMMANDS]
    text = (Path(__file__).parents[1] / "COMMANDS.md").read_text(encoding="utf-8")
    assert "### novel write" in text and "Writing with the Gemini API" in text


def test_hand_paste_path_still_works_after_a_write_run(book, monkeypatch, capsys, fake):
    from test_import import bible_reply
    # nothing written by novel write yet: the old path is unchanged
    code, out = import_run(monkeypatch, capsys, bible_reply())
    assert code == 0 and "BIBLE B001-002  ok" in out


def test_a_batch_whose_bible_is_done_is_preferred_over_a_fresh_smaller_one(book, capsys, fake):
    fake.on(lambda r: r.kind == "images" and r.n == 1, *[ok(block("IMAGES", "c0001", bad_image_two(counts(book)[1])))] * 3)
    assert write(capsys)[0] == 1
    from novel.gemini import prompt as P
    P.generate("tide", 1, 1, "scenes")                       # a fresh batch B001-001: its bible is not done
    before = len(fake.requests)
    code, out = write(capsys)
    assert code == 0, out
    assert [r.kind for r in fake.requests[before:]] == ["images"]          # no second bible call
    assert scene(book, 1)["source"]["batch"] == "B001-002"


# ---------------------------------------------------------------- --retranslate / --restore

# A different, valid Hindi for chapter 1 (same names, retold) so the re-translated chapter differs from the
# fixture original.
HI1_NEW = [
    "चैप्टर एक: बंदरगाह की सुबह",
    "सुबह होने से पहले ही मैरेन घाट पर पहुंच जाती है और हवा में नमक की गंध महसूस करती है। समुद्री चिड़ियां दीवार "
    "के ऊपर चक्कर लगा रही हैं।",
    "वह अपनी लालटेन बुझाकर पानी को देखती रहती है।",
    "टोबायस जाल उठाए उसके पास आता है और कहता है कि आज लहरें कुछ ज्यादा बेचैन हैं।",
    "मैरेन बस सिर हिलाती है।",
    "बूढ़ा हार्ल अपनी नाव के पास बैठा दोनों को देखता है और धीरे से कहता है कि जब पानी इतना शांत दिखे तभी सबसे "
    "ज्यादा डरना चाहिए।",
    "तभी घंटाघर की घंटी तीन बार बजती है और पूरा घाट एकदम चुप हो जाता है।",
]


def narrated_chapter(bd, n, *, status="done", seconds=60.0):
    """Add audio, timing, captions, a QA review file, image files and a manifest row so the chapter looks fully
    narrated. The manifest is created if need be."""
    from novel.stages.manifest import Manifest, Chapter, TRANSLATED, DONE
    root = ep(bd)
    name = f"{n:05d}"
    (root / "audio").mkdir(exist_ok=True)
    (root / "audio" / f"{name}.mp3").write_bytes(b"mp3-bytes")
    (root / "timing").mkdir(exist_ok=True)
    (root / "timing" / f"{name}.json").write_text(
        json.dumps({"total_seconds": seconds, "chunks": [{"para": 0, "start_sec": 0.0, "end_sec": seconds}]}),
        encoding="utf-8")
    (root / "captions").mkdir(exist_ok=True)
    (root / "captions" / f"{name}.json").write_text(
        json.dumps({"version": 2, "chapter": n, "lines": []}), encoding="utf-8")
    (root / "qa").mkdir(exist_ok=True)
    (root / "qa" / f"{name}.json").write_text(
        json.dumps({"version": 2, "chapter": n, "checks": []}), encoding="utf-8")
    (root / "qa" / f"{name}.review.json").write_text(
        json.dumps({"version": 2, "chapter": n, "verdict": "PASS"}), encoding="utf-8")
    img = root / "images" / f"c{n:04d}"
    img.mkdir(parents=True, exist_ok=True)
    (img / "s01-2k.jpg").write_bytes(b"jpg-bytes")
    (img / "s01.prompt.txt").write_text("a prompt", encoding="utf-8")
    man = Manifest.load(root / "manifest.json")
    rows = {c.idx: c for c in man.chapters}
    rows[n] = Chapter(idx=n, title=f"Chapter {n}", source=f"{n:04d}_t.txt", status=status,
                      hindi=f"{name}.txt", audio=f"{name}.mp3", timing=f"{name}.json", seconds=seconds)
    man = Manifest(path=root / "manifest.json", chapters=[rows[i] for i in sorted(rows)])
    man.save()


def content_hashes(root: Path) -> dict:
    """{relative path: sha256 of bytes} for every file under root (no directories)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def manifest_row(bd, n):
    from novel.stages.manifest import Manifest
    man = Manifest.load(ep(bd) / "manifest.json")
    for c in man.chapters:
        if c.idx == n:
            return c
    return None


def hindi1_new_rule(fake):
    fake.on(lambda r: r.kind == "hindi" and r.n == 1, ok("\n\n".join(HI1_NEW)))


def test_retranslate_sets_aside_old_files_and_writes_new_hindi_and_scenes(book, capsys, fake):
    assert write(capsys)[0] == 0                                  # chapters 1 and 2 have scenes
    narrated_chapter(book, 1)                                     # chapter 1 is now fully narrated
    old_hindi = (ep(book) / "hindi" / "00001.txt").read_bytes()
    old_scene = scene(book, 1)
    old_lock = json.loads((ep(book) / "hindi.lock.json").read_text(encoding="utf-8"))
    old_manifest = asdict if False else None                      # placeholder; read below
    from dataclasses import asdict as _asdict
    old_manifest = _asdict(manifest_row(book, 1))
    before = content_hashes(ep(book))
    n_before = len(fake.requests)
    hindi1_new_rule(fake)
    code, out = write(capsys, "--retranslate", "1")
    assert code == 0, out
    # the old version moved to old/v1/
    restore = json.loads((ep(book) / "old" / "v1" / "RESTORE.json").read_text(encoding="utf-8"))
    assert restore["chapter"] == 1 and "timestamp" in restore
    assert restore["lock"] == {"00001.txt": old_lock["00001.txt"]}
    assert restore["manifest"] == old_manifest
    for rel in ("hindi/00001.txt", "audio/00001.mp3", "timing/00001.json", "captions/00001.json",
                "scenes/00001.json", "qa/00001.json", "qa/00001.review.json", "images/c0001"):
        assert rel in restore["files"], rel
    assert (ep(book) / "old" / "v1" / "hindi" / "00001.txt").read_bytes() == old_hindi
    # nothing is deleted: every old content hash still exists somewhere under the episode folder
    after = content_hashes(ep(book))
    # hindi.lock.json and manifest.json are edited in place by the spec; their old entry and row are kept in
    # RESTORE.json (asserted above), so only the moved files must still exist byte-for-byte.
    in_place = {"hindi.lock.json", "manifest.json"}
    missing = sorted(p for p, h in before.items() if p not in in_place and h not in set(after.values()))
    assert not missing, f"these file contents vanished: {missing}"
    # the new Hindi differs and is locked
    new_hindi = (ep(book) / "hindi" / "00001.txt").read_text(encoding="utf-8")
    assert new_hindi == "\n\n".join(HI1_NEW) + "\n"
    assert new_hindi.encode() != old_hindi
    from novel.gemini import prompt as P
    assert P.hindi_state(ep(book), 1) == "locked"
    lock = json.loads((ep(book) / "hindi.lock.json").read_text(encoding="utf-8"))
    assert lock["00001.txt"] == P.sha256(ep(book) / "hindi" / "00001.txt")
    # scenes were written for the new text
    assert S.scenes_path(ep(book), 1).exists()
    new_scene = scene(book, 1)
    assert new_scene["hindi_sha256"] == lock["00001.txt"]
    assert new_scene["scenes"] != old_scene["scenes"] or new_scene["hindi_sha256"] != old_scene["hindi_sha256"]
    # manifest row reset: audio, timing, seconds are gone
    row = manifest_row(book, 1)
    assert row.status == "translated" and row.hindi == "00001.txt"
    assert row.audio is None and row.timing is None and row.seconds is None
    # the warning and the restore command are printed
    assert "chapter 1: a translation already exists. It was moved to" in out
    assert "WARNING the narrated audio of this chapter was set aside" in out
    assert "novel tts push --chapters 1" in out
    assert "to go back: novel write --restore 1" in out
    # a translation call was made for chapter 1
    assert [r.kind for r in fake.requests[n_before:] if r.kind == "hindi"] == ["hindi"]


def test_retranslating_two_chapters_makes_one_recap_at_the_end(book, capsys, fake):
    assert write(capsys)[0] == 0
    before = fake.count("recap")
    hindi1_new_rule(fake)
    assert write(capsys, "--retranslate", "1-2")[0] == 0
    assert fake.count("recap") == before + 1


def test_recap_dry_run_sends_nothing_and_keeps_the_old_recap(book3, capsys, fake):
    assert write(capsys)[0] == 0
    f = ep(book3) / "recap.txt"
    old, before = f.read_text(encoding="utf-8"), fake.count("recap")
    code, out = write(capsys, "--recap", "--dry-run")
    assert code == 0 and "Dry run" in out
    assert fake.count("recap") == before and f.read_text(encoding="utf-8") == old


def test_restore_after_retranslate_brings_back_the_originals_byte_identical(book, capsys, fake):
    assert write(capsys)[0] == 0
    narrated_chapter(book, 1)
    old_hindi = (ep(book) / "hindi" / "00001.txt").read_bytes()
    old_scene_bytes = S.scenes_path(ep(book), 1).read_bytes()
    old_audio = (ep(book) / "audio" / "00001.mp3").read_bytes()
    old_timing = (ep(book) / "timing" / "00001.json").read_bytes()
    old_captions = (ep(book) / "captions" / "00001.json").read_bytes()
    old_qa = (ep(book) / "qa" / "00001.json").read_bytes()
    old_qa_review = (ep(book) / "qa" / "00001.review.json").read_bytes()
    old_lock = json.loads((ep(book) / "hindi.lock.json").read_text(encoding="utf-8"))
    old_manifest = (ep(book) / "manifest.json").read_bytes()
    old_img = (ep(book) / "images" / "c0001" / "s01-2k.jpg").read_bytes()
    hindi1_new_rule(fake)
    assert write(capsys, "--retranslate", "1")[0] == 0
    new_hindi = (ep(book) / "hindi" / "00001.txt").read_bytes()      # the re-translated version
    # restore
    code, out = write(capsys, "--restore", "1")
    assert code == 0, out
    # every original file is byte-identical
    assert (ep(book) / "hindi" / "00001.txt").read_bytes() == old_hindi
    assert S.scenes_path(ep(book), 1).read_bytes() == old_scene_bytes
    assert (ep(book) / "audio" / "00001.mp3").read_bytes() == old_audio
    assert (ep(book) / "timing" / "00001.json").read_bytes() == old_timing
    assert (ep(book) / "captions" / "00001.json").read_bytes() == old_captions
    assert (ep(book) / "qa" / "00001.json").read_bytes() == old_qa
    assert (ep(book) / "qa" / "00001.review.json").read_bytes() == old_qa_review
    assert (ep(book) / "manifest.json").read_bytes() == old_manifest
    assert (ep(book) / "images" / "c0001" / "s01-2k.jpg").read_bytes() == old_img
    # the lock entry is back and the lock check passes
    lock = json.loads((ep(book) / "hindi.lock.json").read_text(encoding="utf-8"))
    assert lock["00001.txt"] == old_lock["00001.txt"]
    from novel.stages import episode as E
    E.check_lock(ep(book), 1)
    # the re-translated version now sits in a newer old/v folder
    new_v = json.loads((ep(book) / "old" / "v2" / "RESTORE.json").read_text(encoding="utf-8"))
    assert new_v["chapter"] == 1
    assert (ep(book) / "old" / "v2" / "hindi" / "00001.txt").read_bytes() == new_hindi
    assert "restored the old version from old/v1/" in out


def test_retranslate_failure_restores_the_old_version_and_returns_nonzero(book, capsys, fake):
    assert write(capsys)[0] == 0
    narrated_chapter(book, 1)
    old_hindi = (ep(book) / "hindi" / "00001.txt").read_bytes()
    old_scene_bytes = S.scenes_path(ep(book), 1).read_bytes()
    old_lock = json.loads((ep(book) / "hindi.lock.json").read_text(encoding="utf-8"))
    old_manifest = (ep(book) / "manifest.json").read_bytes()
    # the fake server fails the translation: never-Hindi text on every attempt
    fake.on(lambda r: r.kind == "hindi" and r.n == 1, *[ok("not Hindi at all, just English words")] * 5)
    code, out = write(capsys, "--retranslate", "1")
    assert code != 0
    # the original version is back in place and intact
    assert (ep(book) / "hindi" / "00001.txt").read_bytes() == old_hindi
    assert S.scenes_path(ep(book), 1).read_bytes() == old_scene_bytes
    lock = json.loads((ep(book) / "hindi.lock.json").read_text(encoding="utf-8"))
    assert lock == old_lock
    assert (ep(book) / "manifest.json").read_bytes() == old_manifest
    assert "re-translate failed; the old version is back" in out
    # nothing was left set aside (the old folder was emptied back)
    assert not (ep(book) / "old" / "v1").exists() or not any((ep(book) / "old" / "v1").iterdir())


def test_translate_a_chapter_without_hindi_translates_it_and_sets_nothing_aside(book3, capsys, fake):
    # --translate works on a chapter with no translation yet: it is simply translated, nothing is set aside
    code, out = write(capsys, "--translate", "3")
    assert code == 0, out
    assert (ep(book3) / "hindi" / "00003.txt").exists()
    assert [r.kind for r in fake.requests if r.kind == "hindi" and r.n == 3]
    assert not (ep(book3) / "old").exists()
    assert "a translation already exists" not in out


def test_retranslate_refuses_a_published_episode(book, capsys, fake):
    (ep(book) / "episode.yaml").write_text("published: true\n", encoding="utf-8")
    code, out = write(capsys, "--retranslate", "1")
    assert code != 0
    assert "published" in out and "refused" in out
    assert fake.requests == []


def test_restore_with_nothing_to_restore_is_a_clear_error(book, capsys, fake):
    code, out = write(capsys, "--restore", "1")
    assert code != 0
    assert "nothing to restore" in out and "chapter 1" in out
    assert fake.requests == []


def test_retranslate_dry_run_writes_nothing_and_makes_no_requests(book, capsys, fake):
    assert write(capsys)[0] == 0
    narrated_chapter(book, 1)
    before = content_hashes(ep(book))
    n_before = len(fake.requests)                                 # the setup write above made requests
    code, out = write(capsys, "--retranslate", "1", "--dry-run")
    assert code == 0, out
    assert fake.requests[n_before:] == []
    assert content_hashes(ep(book)) == before
    assert "would set aside" in out and "novel write --restore 1" in out



# ---------------------------------------------------------------- a good translation is kept when the scenes fail

def test_retranslate_keeps_a_good_translation_when_the_scenes_step_fails_and_a_plain_run_finishes_it(
        book, capsys, fake):
    assert write(capsys)[0] == 0
    narrated_chapter(book, 1)
    old_hindi = (ep(book) / "hindi" / "00001.txt").read_bytes()
    count = counts(book)[1]
    hindi1_new_rule(fake)
    fake.on(lambda r: r.kind == "images" and r.n == 1,
            *[ok(block("IMAGES", "c0001", bad_image_two(count)))] * 3)          # the scenes step never passes
    code, out = write(capsys, "--translate", "1")
    assert code != 0
    assert "chapter 1: translated; scenes are not complete (" in out
    assert "). Run: novel write --chapters 1" in out
    assert "re-translate failed; the old version is back" not in out
    from novel.gemini import prompt as P
    assert (ep(book) / "hindi" / "00001.txt").read_text(encoding="utf-8") == "\n\n".join(HI1_NEW) + "\n"
    assert P.hindi_state(ep(book), 1) == "locked"                                # new translation kept and locked
    assert (ep(book) / "old" / "v1" / "hindi" / "00001.txt").read_bytes() == old_hindi
    assert (ep(book) / "old" / "v1" / "RESTORE.json").exists()
    assert not S.scenes_path(ep(book), 1).exists()
    before = len(fake.requests)
    code, out = write(capsys, "--chapters", "1")                                 # resumes with the scenes step only
    assert code == 0, out
    new = fake.requests[before:]
    assert "hindi" not in {r.kind for r in new} and [r.kind for r in new].count("images") >= 1
    assert S.check_chapter(ep(book), 1) == [] and len(scene(book, 1)["scenes"]) == count


# ---------------------------------------------------------------- an IMAGES reply with a few bad lines is salvaged

def broken_lines(count, bad, chapter=1):
    """The block body of a chapter as text, the image lines of the windows in `bad` made unreadable."""
    out = []
    for o in chapter_lines(count, chapter):
        if o["type"] == "image" and o["n"] in bad:
            out.append('{"type": "image", "n": %d, "group": "%s", "mood><relaxed", "shot": }' % (o["n"], o["group"]))
        else:
            out.append(json.dumps(o, ensure_ascii=False))
    return "\n".join(out)


def test_a_few_unreadable_lines_are_dropped_and_only_those_windows_are_asked_again(book, capsys, fake):
    count = counts(book)[1]
    assert count >= 4
    bad = {2, 3}
    fake.on(lambda r: r.kind == "images" and r.n == 1, ok(block("IMAGES", "c0001", broken_lines(count, bad))))
    fake.on(lambda r: r.kind == "images" and r.n == 1 and r.rest,
            lambda r: ok(block("IMAGES", "c0001", [o for o in chapter_lines(count, 1)
                                                     if o["type"] == "image" and o["n"] in bad])))
    code, out = write(capsys)
    assert code == 0, out
    assert fake.count("images", 1) == 2
    assert "dropped 2 lines that are not valid JSON (windows 2-3)" in out
    first, second = [r for r in fake.requests if r.kind == "images" and r.n == 1]
    assert second.rest and "missing windows 2-3" in second.user
    assert "Do not repeat earlier lines" in second.user
    assert second.user.count('"type": "image"') == 2                  # only the two bad lines, not the whole block
    # the lines already accepted are not sent again: at most the bad lines themselves
    accepted = [json.dumps(o, ensure_ascii=False) for o in chapter_lines(count, 1)
                if o["type"] == "image" and o["n"] not in bad]
    assert all(l not in second.user for l in accepted)
    assert "mood><relaxed" in second.user
    assert len(scene(book, 1)["scenes"]) == count and S.check_chapter(ep(book), 1) == []


def test_a_reply_where_every_line_is_unreadable_is_still_rejected(book, capsys, fake):
    count = counts(book)[1]
    junk = "\n".join('{"type": "image", "n": %d, "mood><relaxed", }' % k for k in range(1, count + 1))
    fake.on(lambda r: r.kind == "images" and r.n == 1, *[ok(block("IMAGES", "c0001", junk))] * 3)
    code, out = write(capsys)
    assert code == 1, out
    assert fake.count("images", 1) == 3 and "dropped" not in out
    assert not S.scenes_path(ep(book), 1).exists()
    assert len(scene(book, 2)["scenes"]) == counts(book)[2]


# ---------------------------------------------------------------- the translation length cap

def hindi_words(n):
    """A narration of n words (the heading line not counted), with the names in it, in paragraphs of 60 words."""
    vocab = ["मैरेन", "टोबायस", "दीपक", "घंटी", "कोहरा", "नाव", "घाट", "रात"]
    words = [vocab[i % len(vocab)] for i in range(n)]
    return [HI3[0]] + [" ".join(words[k:k + 60]) + "।" for k in range(0, n, 60)]


def joined(paras):
    return "\n\n".join(paras)


def words_book(tmp_path, monkeypatch, chapter3=True):
    """The real defaults of the translate settings: 800 words, tolerance 0.20 (640 to 960), 4 attempts."""
    return make_book(tmp_path, monkeypatch, chapter3=chapter3, settings={
        "translate": {"target_words": 800, "words_tolerance": 0.20, "max_attempts": 4}})


def hindi_requests(fake):
    return [r for r in fake.requests if r.kind == "hindi"]


def test_a_narration_outside_the_band_is_asked_again_with_its_count_and_the_third_inside_is_kept(
        tmp_path, monkeypatch, capsys, fake, clock):
    bd = words_book(tmp_path, monkeypatch)
    fake.on(lambda r: r.kind == "hindi", ok(joined(hindi_words(1900))), ok(joined(hindi_words(300))),
            ok(joined(hindi_words(820))))
    code, out = write(capsys, "--chapters", "3")
    assert code == 0, out
    assert fake.count("hindi") == 3
    req = hindi_requests(fake)
    assert "too long: 1900 words; write between 640 and 960 words. Cut descriptions and side information first; " \
           "keep the events and the action." in req[1].user
    assert "too short: 300 words; write between 640 and 960 words. Put back the events, decisions and dialogue you " \
           "cut; keep action scenes move by move." in req[2].user
    assert "Write the whole narration again" in req[2].user
    stored = (ep(bd) / "hindi" / "00003.txt").read_text(encoding="utf-8").split()
    assert len(stored) - 3 == 820                             # the heading line is not counted
    assert "820 words (band 640-960)" in out


def test_all_four_attempts_far_out_fail_the_step_and_nothing_is_locked(tmp_path, monkeypatch, capsys, fake, clock):
    bd = words_book(tmp_path, monkeypatch)
    fake.on(lambda r: r.kind == "hindi", ok(joined(hindi_words(1900))), ok(joined(hindi_words(300))),
            ok(joined(hindi_words(250))), ok(joined(hindi_words(2000))))
    code, out = write(capsys, "--chapters", "3")
    assert fake.count("hindi") == 4
    assert not (ep(bd) / "hindi" / "00003.txt").exists()
    assert "accepted" not in out and code != 0


def test_all_four_attempts_far_out_in_a_translate_run_restore_the_old_version(tmp_path, monkeypatch, capsys, fake,
                                                                             clock):
    bd = words_book(tmp_path, monkeypatch)
    fake.on(lambda r: r.kind == "hindi", ok(joined(hindi_words(800))))
    assert write(capsys, "--chapters", "3")[0] == 0
    old_hindi = (ep(bd) / "hindi" / "00003.txt").read_bytes()
    old_lock = json.loads((ep(bd) / "hindi.lock.json").read_text(encoding="utf-8"))
    fake.on(lambda r: r.kind == "hindi" and r.n == 3, ok(joined(hindi_words(1900))), ok(joined(hindi_words(300))),
            ok(joined(hindi_words(250))), ok(joined(hindi_words(2000))))
    code, out = write(capsys, "--translate", "3")
    assert code != 0
    assert (ep(bd) / "hindi" / "00003.txt").read_bytes() == old_hindi
    assert json.loads((ep(bd) / "hindi.lock.json").read_text(encoding="utf-8")) == old_lock
    assert "re-translate failed; the old version is back" in out


def test_a_narration_between_the_two_nearest_attempts_is_taken_with_a_warning(tmp_path, monkeypatch, capsys, fake,
                                                                             clock):
    bd = words_book(tmp_path, monkeypatch)
    fake.on(lambda r: r.kind == "hindi", ok(joined(hindi_words(1900))), ok(joined(hindi_words(300))),
            ok(joined(hindi_words(1000))), ok(joined(hindi_words(1100))))
    code, out = write(capsys, "--chapters", "3")
    assert code == 0, out
    assert fake.count("hindi") == 4
    assert "accepted outside the band 640-960 words: too long: 1000 words" in out
    stored = (ep(bd) / "hindi" / "00003.txt").read_text(encoding="utf-8").split()
    assert len(stored) - 3 == 1000
    qa = json.loads((ep(bd) / "qa" / "00003.json").read_text(encoding="utf-8"))
    assert any("accepted outside the band" in w for w in qa["warnings"])


def test_the_narration_rules_carry_the_word_target_and_band_from_the_settings(tmp_path, monkeypatch, capsys, fake,
                                                                             clock):
    words_book(tmp_path, monkeypatch)
    write(capsys, "--chapters", "3")
    system = next(r.system for r in fake.requests if r.kind == "hindi")
    assert "at most 960 Hindi words (aim for about 800, and at least 640)" in " ".join(system.split())
    assert "move by move, in order" in system
    low = system.lower()
    assert "no word limit" not in low and "never drop information to make it shorter" not in low
    assert "never more than" not in low and "% of the english" not in low and "characters without spaces" not in low


def test_a_book_word_target_of_600_changes_the_band_in_the_rules(tmp_path, monkeypatch, capsys, fake, clock):
    make_book(tmp_path, monkeypatch, chapter3=True, settings={
        "translate": {"target_words": 600, "words_tolerance": 0.20}})
    fake.on(lambda r: r.kind == "hindi", ok(joined(hindi_words(600))))
    code, out = write(capsys, "--chapters", "3")
    assert code == 0, out
    system = next(r.system for r in fake.requests if r.kind == "hindi")
    assert "at most 720 Hindi words (aim for about 600, and at least 480)" in " ".join(system.split())


def test_a_run_that_translates_makes_a_recap_of_the_episode_and_a_rerun_makes_no_call(book3, capsys, fake):
    code, out = write(capsys)
    assert code == 0, out
    rq = next(r for r in fake.requests if r.kind == "recap")
    got = len((ep(book3) / "recap.txt").read_text(encoding="utf-8").split())
    assert fake.count("recap") == 1 and 60 <= got <= 90
    assert "THE RECAP" in rq.system and "BIBLE" not in rq.system and "THE EPISODE" in rq.user
    assert "novel intro init --ep 2 --text-file" in out
    n = fake.count()
    write(capsys)
    assert fake.count() == n


def test_a_bad_recap_is_asked_again_and_recap_makes_only_the_recap(book3, capsys, fake):
    fake.on(lambda r: r.kind == "recap", ok("बहुत छोटा"))
    code, out = write(capsys)
    assert code == 0, out
    assert fake.count("recap") == 2 and "recap: rejected" in out
    before = fake.count()
    code, out = write(capsys, "--recap")
    assert code == 0, out
    assert fake.count() == before + 1 and fake.count("recap") == 3


def test_every_gemini_key_limited_asks_openrouter_for_the_same_model(book, fake, monkeypatch, tmp_path):
    from novel import korouter
    from novel.stages import episode_translate as T
    fake.limited_keys = {KEY1, KEY2}
    seen = {}

    class Chat:
        def __init__(self, key, model):
            seen.update(key=key, model=model)

        def generate(self, system, text, **kw):
            return T.GenResult("from openrouter", "STOP", "google/gemini-test", 0,
                               {"promptTokenCount": 3, "candidatesTokenCount": 2})

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(korouter, "Chat", Chat)
    g = T.Gemini("gemini-test", tmp_path / "c.jsonl", sleep=lambda s: None, say=lambda t: None)
    r = g.generate("system", "hello")
    assert r.text == "from openrouter" and seen == {"key": "or-test-key", "model": "google/gemini-test"}
    assert len(fake.requests) == 2 and g.usage["output"] == 2           # each Gemini key once, then OpenRouter


def test_without_an_openrouter_key_a_limited_run_still_waits(book, fake, monkeypatch, tmp_path):
    from novel.stages import episode_translate as T
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    fake.limited_keys = {KEY1, KEY2}
    g = T.Gemini("gemini-test", tmp_path / "c.jsonl", sleep=lambda s: None, say=lambda t: None)
    with pytest.raises(T.QuotaExhausted):
        g.generate("system", "hello")
