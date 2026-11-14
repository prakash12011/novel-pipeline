"""Two-step English captions: text from the locked Hindi alone, timing applied later.

No network: Gemini calls go to a local HTTP server through NOVEL_GEMINI_URL, or to a fake client.
All Hindi here is invented.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from novel import cli
from novel.core import context, paths
from novel.stages import episode as E
from novel.stages import episode_captions as C
from novel.stages.manifest import Chapter, Manifest

REPO = Path(__file__).resolve().parents[1]

LONG = " ".join(f"यह वाक्य नंबर {i} है और कहानी आगे बढ़ती जाती है।" for i in range(1, 11))   # > 300 chars: 2+ chunks
TEXT = {
    1: "चैप्टर 1: नीली घाटी\n\nनदी के किनारे एक छोटा सा घर था। उसमें एक बूढ़ा माली रहता था।\n\n"
       "हर सुबह वह फूलों को पानी देता था! शाम को वह तारों को गिनता था?\n\n" + LONG,
    2: "चैप्टर 2: चाँदी की चाबी\n\nएक दिन माली को मिट्टी में एक चमकती चाबी मिली।\n\n"
       "उसने सोचा कि यह किस दरवाज़े की होगी। फिर वह हँसा और काम में लग गया।",
    3: "चैप्टर 3: बंद दरवाज़ा\n\nदरवाज़ा बहुत पुराना था।",     # never locked
}
BOOK = "tb"


# ------------------------------------------------------------------ fake Gemini endpoint

class _Handler(BaseHTTPRequestHandler):
    state = {"mode": "ok", "systems": [], "inputs": []}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["system_instruction"]["parts"][0]["text"]
        rows = json.loads(body["contents"][0]["parts"][0]["text"])
        self.state["systems"].append(system)
        self.state["inputs"].append(rows)
        n = len(rows) - (1 if self.state["mode"] == "short" else 0)
        text = json.dumps({"en": [f"English {i}" for i in range(n)]})
        out = json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    _Handler.state = {"mode": "ok", "systems": [], "inputs": []}
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NOVEL_GEMINI_URL", f"http://127.0.0.1:{srv.server_port}/{{model}}")
    yield _Handler.state
    srv.shutdown()


class FakeGemini:
    model = "fake-model"

    def __init__(self):
        self.systems = []

    def ask(self, system, text, **kw):
        self.systems.append(system)
        return json.dumps({"en": [f"Line {i}" for i in range(len(json.loads(text)))]})


# ------------------------------------------------------------------ fixture episode

@pytest.fixture
def env(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    (home / "secrets.env").write_text("GEMINI_API_KEY=not-a-real-key\n", encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    old = E.EPISODES
    context.activate(BOOK)
    root = paths.ep_root(BOOK, 1)
    (root / "hindi").mkdir(parents=True)
    chapters = []
    for n, text in TEXT.items():
        (root / "hindi" / f"{n:05d}.txt").write_text(text, encoding="utf-8")
        chapters.append(Chapter(idx=n, title=text.split("\n")[0], source=f"{n:04d}_x.txt"))
    Manifest(path=root / "manifest.json", chapters=chapters).save()
    (root / "episode.json").write_text(json.dumps({"episode": 1, "start": 1, "end": 3}))
    from novel.stages.episode_translate import cmd_lock
    cmd_lock(SimpleNamespace(episode=1, chapters="1,2"))
    yield SimpleNamespace(home=home, data=data, root=root, book=data / BOOK)
    E.EPISODES = old
    context._ACTIVE_BOOK = None


def write_timing(root, n, text=None):
    """Invented timing map for the chapter's chunks: 2.0 s of speech per chunk, 0.5 s apart."""
    text = text if text is not None else (root / "hindi" / f"{n:05d}.txt").read_text(encoding="utf-8")
    k = len(C.chunks(text))
    (root / "timing").mkdir(exist_ok=True)
    chunks = [{"start_sec": 0.5 + i * 2.5, "end_sec": 2.5 + i * 2.5 + 0.37 * (i % 3)} for i in range(k)]
    (root / "timing" / f"{n:05d}.json").write_text(json.dumps({"chunks": chunks}), encoding="utf-8")
    return chunks


def write_bible(env):
    d = env.book / "bible"
    d.mkdir(parents=True, exist_ok=True)
    (d / "characters.json").write_text(json.dumps({"version": 2, "style": "", "content_flags": {}, "characters": {
        "mira": {"display": "Mira Vale", "hi": "मीरा वेल"},
        "tor": {"display": "Tor", "hi": "तोर"}}}, ensure_ascii=False), encoding="utf-8")
    (d / "names.json").write_text(json.dumps({"Old Gardener": "बूढ़ा माली", "Mira Vale": "मीरा वेल"},
                                             ensure_ascii=False), encoding="utf-8")


def set_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def captions_cmd(*argv):
    return cli.main(["captions", "--book", BOOK, "--ep", "1", *argv])


# ------------------------------------------------------------------ the old one-step algorithm (private copy)

def old_timed_sentences(text, timing):
    """abpipe/episode_captions.timed_sentences as it was, taking the text and the timing list."""
    cs = C.chunks(text)
    assert len(cs) == len(timing)
    out = []
    for (pi, ss), t in zip(cs, timing):
        total = sum(len(s) for s in ss)
        at = t["start_sec"]
        for s in ss:
            dur = (t["end_sec"] - t["start_sec"]) * len(s) / total
            out.append({"para": pi, "hi": s, "start": round(at, 3), "end": round(at + dur, 3)})
            at += dur
    return out


# ------------------------------------------------------------------ translate: text only

def test_translate_needs_no_timing(env, server):
    assert not (env.root / "timing").exists()
    out = C.translate_chapter(env.root, 1, C.make_gemini(env.root, "m"))
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == 2 and doc["chapter"] == 1 and doc["model"] == "m"
    assert doc["hindi_sha256"] == E.sha256(env.root / "hindi" / "00001.txt")
    assert doc["chunks"] == len(C.chunks(TEXT[1])) >= 4
    assert all("start" not in r and "end" not in r for r in doc["lines"])
    first = doc["lines"][0]
    assert first == {"para": 0, "chunk": 0, "off": 0, "len": len(first["hi"]), "hi": first["hi"], "en": "English 0"}
    # offsets locate each sentence in its chunk's text
    chunk_text = {i: " ".join(ss) for i, (_, ss) in enumerate(C.chunks(TEXT[1]))}
    for r in doc["lines"]:
        assert chunk_text[r["chunk"]][r["off"]:r["off"] + r["len"]] == r["hi"]


def test_sentences_align_with_the_tts_chunker():
    sys.path.insert(0, str(REPO / "kaggle" / "payload"))
    try:
        from abpipe.tts import chunk_hindi
    except Exception as e:                                   # kernel-only dependencies missing
        pytest.skip(f"TTS chunker not importable here: {e}")
    finally:
        sys.path.pop(0)
    for text in TEXT.values():
        assert [" ".join(ss) for _, ss in C.chunks(text)] == [c.text for c in chunk_hindi(text, C.CHUNK_CHARS)]
        assert [pi for pi, _ in C.chunks(text)] == [c.para for c in chunk_hindi(text, C.CHUNK_CHARS)]


# ------------------------------------------------------------------ identity with the old code

def test_two_steps_equal_the_old_one_step_result(env, server):
    text = TEXT[1]
    timing = write_timing(env.root, 1)
    old = old_timed_sentences(text, timing)
    C.translate_chapter(env.root, 1, C.make_gemini(env.root))
    new = C.timed_lines(env.root, 1)
    assert [(r["para"], r["hi"], r["start"], r["end"]) for r in new] == \
           [(r["para"], r["hi"], r["start"], r["end"]) for r in old]
    assert [r["en"] for r in new] == [f"English {i}" for i in range(len(old))]
    # the ASS file made from the new two-step path == the one made from an old-format file
    new_ass = env.root / "new.ass"
    C.write_ass(env.root, 1, new_ass)
    doc = json.loads(C.caption_path(env.root, 1).read_text(encoding="utf-8"))
    legacy = [{**o, "en": r["en"]} for o, r in zip(old, doc["lines"])]
    C.caption_path(env.root, 1).write_text(json.dumps({"chapter": 1, "model": "x", "lines": legacy}), encoding="utf-8")
    old_ass = env.root / "old.ass"
    C.write_ass(env.root, 1, old_ass)
    assert new_ass.read_text(encoding="utf-8") == old_ass.read_text(encoding="utf-8")
    assert new_ass.read_text(encoding="utf-8").count("Dialogue:") > 5


def test_timed_sentences_is_the_old_function(env):
    timing = write_timing(env.root, 2)
    assert [(r["para"], r["hi"], r["start"], r["end"]) for r in C.timed_sentences(env.root, 2)] == \
           [(r["para"], r["hi"], r["start"], r["end"]) for r in old_timed_sentences(TEXT[2], timing)]


def test_wrong_chunk_count_is_refused(env):
    write_timing(env.root, 1)
    t = env.root / "timing" / "00001.json"
    doc = json.loads(t.read_text())
    doc["chunks"].pop()
    t.write_text(json.dumps(doc))
    with pytest.raises(SystemExit, match="chunks in the text"):
        C.timed_sentences(env.root, 1)


# ------------------------------------------------------------------ staleness

def test_stale_hindi_is_redone_by_translate_and_refused_by_time(env, server):
    C.ensure(env.root, 2)
    write_timing(env.root, 2)
    assert not C.stale(env.root, 2)
    C.write_ass(env.root, 2, env.root / "x.ass")                       # fine while current
    f = env.root / "hindi" / "00002.txt"
    f.write_text(TEXT[2] + "\n\nएक नया पैरा।", encoding="utf-8")
    assert C.stale(env.root, 2)
    write_timing(env.root, 2)
    with pytest.raises(SystemExit) as e:                                # time/burn refuses and names the fix
        C.write_ass(env.root, 2, env.root / "x.ass")
    assert "novel captions --ep 1 --chapters 2 --force" in str(e.value)
    before = len(server["inputs"])
    C.ensure(env.root, 2)                                               # translate redoes it
    assert len(server["inputs"]) == before + 1 and not C.stale(env.root, 2)
    assert json.loads(C.caption_path(env.root, 2).read_text(encoding="utf-8"))["hindi_sha256"] == E.sha256(f)
    C.write_ass(env.root, 2, env.root / "x.ass")


def test_time_without_a_timing_map_says_what_is_missing(env, server):
    C.ensure(env.root, 1)
    with pytest.raises(SystemExit, match="no timing map yet"):
        C.write_ass(env.root, 1, env.root / "x.ass")


def test_old_format_file_is_used_untouched(env, server):
    timing = write_timing(env.root, 2)
    rows = [{**r, "en": f"Old line {i}"} for i, r in enumerate(old_timed_sentences(TEXT[2], timing))]
    p = C.caption_path(env.root, 2)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({"chapter": 2, "model": "old", "lines": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    raw = p.read_bytes()
    # even when the Hindi on disk no longer matches (nothing to compare with), it is current
    (env.root / "hindi" / "00002.txt").write_text(TEXT[2] + " और भी।", encoding="utf-8")
    assert C.is_legacy(C.load(env.root, 2)) and not C.stale(env.root, 2)
    assert C.ensure(env.root, 2) == p and not server["inputs"]
    ass = env.root / "o.ass"
    C.write_ass(env.root, 2, ass)
    assert "Old line 2" in ass.read_text(encoding="utf-8") and p.read_bytes() == raw


# ------------------------------------------------------------------ names

def test_names_come_from_the_book_bible(env, server):
    write_bible(env)
    C.translate_chapter(env.root, 2, C.make_gemini(env.root))
    system = server["systems"][-1]
    assert "Keep names as the English spellings below." in system
    assert "NAMES: Mira Vale, Tor, Old Gardener." in system             # deduplicated, display names first
    assert "Madame Pualis" not in system and "Reimund" not in system     # the old hardcoded list is gone


def test_no_bible_means_no_names(env, server):
    C.translate_chapter(env.root, 2, C.make_gemini(env.root))
    system = server["systems"][-1]
    assert "NAMES" not in system and "names" not in system.lower()
    assert "EXACTLY one English line per input" in system               # the rest of the prompt is there
    assert C.book_names("", book="nobook") == []


def test_long_name_lists_keep_only_names_in_the_chapter(env, monkeypatch):
    write_bible(env)
    monkeypatch.setattr(C, "NAME_LIMIT", 2)
    assert C.book_names("वहाँ बूढ़ा माली था।") == ["Old Gardener"]
    assert sorted(C.book_names("मीरा वेल और तोर")) == ["Mira Vale", "Tor"]


# ------------------------------------------------------------------ font, size, line length

def style_line(path):
    return next(l for l in Path(path).read_text(encoding="utf-8").splitlines() if l.startswith("Style:")).split(",")


def made(env, server, n=1):
    C.ensure(env.root, n)
    write_timing(env.root, n)


def test_ass_style_uses_the_bundled_font_family(env, server):
    made(env, server)
    C.write_ass(env.root, 1, env.root / "a.ass")
    st = style_line(env.root / "a.ass")
    assert st[1] == "Noto Sans SemiBold" and st[2] == "38"
    # the family comes from the font file itself, not from the (here wrong) font_name setting
    set_yaml(env.book / "book.yaml", {"video": {"captions": {"font_name": "Bogus Font"}}})
    C.write_ass(env.root, 1, env.root / "b.ass")
    assert style_line(env.root / "b.ass")[1] == "Noto Sans SemiBold"
    assert C.font_family(REPO / "assets" / "fonts" / "PlayfairDisplay.ttf") == "Playfair Display"


def test_size_and_line_length_from_settings_with_chapter_override(env, server):
    made(env, server, 1)
    made(env, server, 2)
    server_en = "word " * 14                                              # 70 characters
    for n in (1, 2):                                                      # one long English line each
        p = C.caption_path(env.root, n)
        doc = json.loads(p.read_text(encoding="utf-8"))
        for r in doc["lines"]:
            r["en"] = server_en.strip()
        p.write_text(json.dumps(doc), encoding="utf-8")
    set_yaml(env.book / "book.yaml", {"video": {"captions": {"size": 44, "max_line": 40}}})
    set_yaml(env.root / "episode.yaml", {"chapters": {2: {"video": {"captions": {"size": 60, "max_line": 25}}}}})
    outs = {}
    for n in (1, 2):
        ass = env.root / f"c{n}.ass"
        C.write_ass(env.root, n, ass)
        outs[n] = ass
    assert style_line(outs[1])[2] == "44" and style_line(outs[2])[2] == "60"
    for n, limit in ((1, 40), (2, 25)):
        texts = [l.split(",", 9)[9] for l in outs[n].read_text(encoding="utf-8").splitlines() if l.startswith("Dialogue")]
        assert texts and max(map(len, texts)) <= limit
    assert max(len(l.split(",", 9)[9]) for l in outs[1].read_text(encoding="utf-8").splitlines()
               if l.startswith("Dialogue")) > 25                          # chapter 1 really used 40


def test_filter_string_escapes_drive_colon_backslashes_and_spaces():
    if os.name != "nt":
        pytest.skip("drive letters only exist on Windows")
    f = C.ass_filter(Path(r"C:\My Videos\Novel Pipeline\c0001.en.ass"), Path(r"D:\Novel Pipeline\assets\fonts"))
    assert f == "ass='C\\:/My Videos/Novel Pipeline/c0001.en.ass':fontsdir='D\\:/Novel Pipeline/assets/fonts'"
    assert "\\\\" not in f.replace("\\:", "")                             # no backslash left except before a colon


def test_default_filter_points_at_the_bundled_fonts():
    f = C.ass_filter(Path("x.ass"))
    assert f.endswith("assets/fonts'") and "fontsdir=" in f


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


@needs_ffmpeg
def test_real_burn_finds_the_bundled_font(env, server, tmp_path):
    made(env, server, 2)
    ass = tmp_path / "with space" / "c2.en.ass"
    ass.parent.mkdir()
    assert C.write_ass(env.root, 2, ass) > 0
    out = tmp_path / "with space" / "o.mp4"
    cmd = ["ffmpeg", "-v", "verbose", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=3:r=10",
           "-vf", C.chapter_ass_filter(env.root, 2, ass), str(out)]
    p = subprocess.run(cmd, cwd=tmp_path.anchor, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-800:]
    # make sure at least one event shows inside the 3 s: rewrite the events to start at 0.5 s
    ass.write_text(re.sub(r"Dialogue: 0,[\d:.]+,[\d:.]+", "Dialogue: 0,0:00:00.50,0:00:02.50",
                          ass.read_text(encoding="utf-8")), encoding="utf-8")
    p = subprocess.run(cmd, cwd=tmp_path.anchor, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-800:]
    picks = [l for l in p.stderr.splitlines() if "fontselect:" in l]
    assert picks, p.stderr[-800:]
    assert all("(Noto Sans SemiBold," in l and l.rstrip().endswith("-> NotoSans-SemiBold, 0, NotoSans-SemiBold")
               for l in picks), picks


# ------------------------------------------------------------------ the command

def test_command_status_lines_and_exit_codes(env, server, capsys):
    (env.root / "hindi" / "00003.txt").write_text(TEXT[3], encoding="utf-8")
    assert captions_cmd() == 0
    out = capsys.readouterr().out
    assert "chapter 1: made" in out and "chapter 2: made" in out
    assert "chapter 3: skipped: not locked" in out
    assert not C.caption_path(env.root, 3).exists()
    assert C.caption_path(env.root, 1).exists() and not (env.root / "timing").exists()

    assert captions_cmd() == 0                                            # second run: nothing to do
    out = capsys.readouterr().out
    assert out.count("already current") == 2 and "made" not in out

    n = len(server["inputs"])
    assert captions_cmd("--chapters", "2", "--force") == 0                # force redoes just that one
    assert "chapter 2: made" in capsys.readouterr().out and len(server["inputs"]) == n + 1

    assert captions_cmd("--chapters", "1-2,9") == 0
    assert "chapter 9: skipped: not in this episode" in capsys.readouterr().out


def test_command_reports_old_format_as_current(env, server, capsys):
    p = C.caption_path(env.root, 1)
    p.parent.mkdir()
    p.write_text(json.dumps({"chapter": 1, "lines": [{"para": 1, "hi": "क", "en": "k", "start": 0, "end": 1}]}))
    assert captions_cmd("--chapters", "1") == 0
    assert "chapter 1: already current (old format)" in capsys.readouterr().out
    assert not server["inputs"]


def test_command_fails_with_exit_1_when_a_chapter_fails(env, server, capsys):
    server["mode"] = "short"                                              # always one line too few
    assert captions_cmd() == 1
    out = capsys.readouterr().out
    assert "chapter 1: FAILED" in out and "chapter 2: FAILED" in out and "one line per sentence" in out
    assert not C.caption_path(env.root, 1).exists()
    server["mode"] = "ok"
    assert captions_cmd() == 0


def test_command_stale_chapter_is_redone_after_relock(env, server, capsys):
    assert captions_cmd() == 0
    capsys.readouterr()
    (env.root / "hindi" / "00002.txt").write_text(TEXT[2] + "\n\nनया पैरा।", encoding="utf-8")
    from novel.stages.episode_translate import cmd_lock
    cmd_lock(SimpleNamespace(episode=1, chapters="2"))
    assert captions_cmd() == 0
    out = capsys.readouterr().out
    assert "chapter 1: already current" in out and "chapter 2: made" in out


def test_command_disabled_does_nothing(env, server, capsys):
    set_yaml(env.book / "book.yaml", {"video": {"captions": {"enabled": False}}})
    assert captions_cmd() == 0
    assert "switched off" in capsys.readouterr().out
    assert not server["inputs"] and not (env.root / "captions").exists()


def test_translate_takes_an_injected_client(env):
    g = FakeGemini()
    C.translate_chapter(env.root, 2, g)
    assert json.loads(C.caption_path(env.root, 2).read_text(encoding="utf-8"))["model"] == "fake-model"
    assert g.systems and "NAMES" not in g.systems[0]
