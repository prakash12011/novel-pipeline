"""novel ktrial scenes | prompts: the local half of the FLUX.3 klein trial (fixture book 'tide', invented content).

No network: the Gemini client is a fake object injected into cmd_scenes. The production files (episodes/, bible/,
gemini/, inbox/) are hashed before and after every command: the trial may only write under _work/ktrial.
"""

import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from novel import bible as B
from novel import kcomfy
from novel import kprompts as K
from novel.commands import ktrial_cmd
from novel.gemini import prompt as P
from novel.gemini import write as W
from novel.stages import episode_scenes as S
from novel.stages import episode_translate as T
from novel.stages import ktrial
from test_import import block, chapter_lines, counts, ep as ep_dir, full_run, setup_book

HUT = {"type": "location", "key": "signal_hut", "name": "Signal Hut", "kind": "interior", "chapters": "1-2",
       "block": ("A cramped wooden hut at the end of the breakwater. A brass signal lamp on a swivel stand faces "
                 "the sea window, a cot with a grey blanket stands against the back wall, charts are pinned to "
                 "every plank and a small iron stove ticks in the corner."),
       "default_light": "warm lamp light"}


# ---------------------------------------------------------------- helpers

class FakeClient:
    """The part of episode_translate.Gemini that cmd_scenes uses: generate(system, user, temperature=) -> GenResult.
    replies: texts or (text, finish); the last one repeats when they run out."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, system, user, temperature=0.4, **kw):
        self.calls.append(SimpleNamespace(system=system, user=user, temperature=temperature))
        r = self.replies[min(len(self.calls), len(self.replies)) - 1]
        text, finish = r if isinstance(r, tuple) else (r, "STOP")
        return T.GenResult(text, finish, "fake-model", 1, usage={"promptTokenCount": 10, "candidatesTokenCount": 5},
                           seconds=0.1)


def snapshot(bd: Path) -> dict:
    """sha256 of every file of the book outside _work (the production files), plus the empty folders."""
    out = {}
    for p in sorted(bd.rglob("*")):
        rel = p.relative_to(bd).as_posix()
        if rel == "_work" or rel.startswith("_work/"):
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
    return out


def args(**kw):
    base = dict(book="tide", chapter=1, ep=None, max_characters=2, from_production=False, force=False)
    base.update(kw)
    return SimpleNamespace(**base)


def valid_reply(bd, chapter=1, **kw):
    return block("IMAGES", f"c{chapter:04d}", chapter_lines(counts(bd)[chapter], chapter, **kw))


def portrait(path: Path, size=(1376, 768)):
    """A head-and-shoulders stand-in: grey background, a red 'person' in the middle, a white 'mark' bottom right."""
    im = Image.new("RGB", size, (128, 128, 128))
    w, h = size
    for x in range(w // 2 - 80, w // 2 + 80):
        for y in range(h // 4, h * 3 // 4):
            im.putpixel((x, y), (220, 20, 20))
    for x in range(w - 90, w - 10):
        for y in range(h - 90, h - 10):
            im.putpixel((x, y), (255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=95)


@pytest.fixture
def book(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch)
    full_run(monkeypatch, capsys, bd)                      # production scene files for chapters 1 and 2
    for stem in ("maren_vale", "tobias_reed"):
        portrait(bd / "bible" / "flow" / f"{stem}-portrait.jpg")
    capsys.readouterr()
    return bd


def trial_of(bd, chapter=1) -> Path:
    return bd / "_work" / "ktrial" / f"c{chapter:04d}"


def prod_scene(bd, chapter=1) -> dict:
    return json.loads(S.scenes_path(ep_dir(bd), chapter).read_text(encoding="utf-8"))


def trial_scene(bd, chapter=1) -> dict:
    return json.loads((trial_of(bd, chapter) / "scenes" / f"{chapter:05d}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- scenes

def test_scenes_writes_the_trial_file_in_the_shape_of_the_importer(book, capsys):
    before = snapshot(book)
    client = FakeClient(valid_reply(book))
    assert ktrial.cmd_scenes(args(), client=client) == 0
    out = capsys.readouterr().out
    doc, prod = trial_scene(book), prod_scene(book)
    # the same reply through the production importer gives the same groups, scenes and lock hash
    assert list(doc) == list(prod)
    assert doc["version"] == 2 and doc["chapter"] == 1 and doc["pacing"] == prod["pacing"]
    assert doc["hindi_sha256"] == prod["hindi_sha256"] and doc["pacing_params"] == prod["pacing_params"]
    assert doc["groups"] == prod["groups"] and doc["scenes"] == prod["scenes"]
    assert len(doc["scenes"]) == counts(book)[1]
    src = doc["source"]
    assert src["via"] == "ktrial" and src["framing"] == "wide" and src["model"] == "fake-model"
    assert src["written"].endswith("+00:00") and set(src) == {"via", "framing", "model", "written"}
    assert snapshot(book) == before
    # the summary
    assert f"windows: {counts(book)[1]}" in out and "groups: 2" in out
    assert "shots: " in out and "people per picture: 1: " in out and "warnings: " in out


def test_the_wide_rules_the_qwen_note_and_two_people_are_really_sent(book):
    client = FakeClient(valid_reply(book))
    assert ktrial.cmd_scenes(args(max_characters=2), client=client) == 0
    (call,) = client.calls
    assert "The camera is CLOSE in this series" not in call.system        # a phrase only the tight rules have
    assert "(at most 2)" in call.system
    assert ktrial.QWEN_NOTE.strip() in call.user and ktrial.KLEIN_NOTE.strip() not in call.user
    # the user text has the production layout: bible, English chapter, the windows, REQUEST
    n = counts(book)[1]
    assert f"##### CHAPTER 1: {n} WINDOWS #####" in call.user and "CHAPTER 1 (English)" in call.user
    assert "CURRENT BIBLE" in call.user and "key: maren_vale" in call.user
    assert f"Return the IMAGES block of chapter 1: one image line for each of its {n} windows" in call.user
    assert "the bible is read-only" in call.user
    wins = P.chapter_windows(ep_dir(book), 1)
    assert W.window_lines(wins) in call.user and len(wins) == n
    # saved
    g = trial_of(book) / "gemini"
    assert (g / "001.request.txt").read_text(encoding="utf-8").startswith("[system]\n")
    assert (g / "001.reply.txt").read_text(encoding="utf-8") == valid_reply(book)
    assert call.system in (g / "001.request.txt").read_text(encoding="utf-8")


def test_max_characters_option_reaches_the_rules(book):
    client = FakeClient(valid_reply(book))
    ktrial.cmd_scenes(args(max_characters=1), client=client)           # rejected (two people in even windows): fine
    assert "(at most 1)" in client.calls[0].system


def test_rules_args_are_the_runners_except_max_characters(book):
    runner = W.Runner("tide", 1, SimpleNamespace(), out=lambda t: None)
    want = runner.rules_args(1, 1)
    got = ktrial.rules_args("tide", 1, 1, 2)
    assert got.pop("max_characters") == 2 and want.pop("max_characters") == runner.rules_args(1, 1)["max_characters"]
    assert got == want


def test_an_invalid_first_reply_is_retried_with_the_whole_chapter(book, capsys):
    n = counts(book)[1]
    short = block("IMAGES", "c0001", chapter_lines(n, 1, end=n - 1))        # one window short: rejected
    client = FakeClient(short, valid_reply(book))
    assert ktrial.cmd_scenes(args(), client=client) == 0
    assert len(client.calls) == 2
    retry = client.calls[1].user
    assert "Your previous answer was rejected" in retry and "YOUR PREVIOUS ANSWER (rejected):" in retry
    assert "too few images" in retry and short.strip() in retry
    assert client.calls[1].system == client.calls[0].system
    g = trial_of(book) / "gemini"
    assert sorted(p.name for p in g.glob("*.txt")) == ["001.reply.txt", "001.request.txt", "002.reply.txt",
                                                       "002.request.txt"]
    assert (g / "001.reply.txt").read_text(encoding="utf-8") == short
    assert "call 1: rejected" in capsys.readouterr().out and len(trial_scene(book)["scenes"]) == n


def test_a_cut_off_reply_is_retried_for_the_whole_chapter(book):
    n = counts(book)[1]
    cut = block("IMAGES", "c0001", chapter_lines(n, 1, end=2), end=False)
    client = FakeClient((cut, "MAX_TOKENS"), valid_reply(book))
    assert ktrial.cmd_scenes(args(), client=client) == 0
    assert len(client.calls) == 2 and "cut off by the length limit" in client.calls[1].user
    assert len(trial_scene(book)["scenes"]) == n


def test_a_bible_block_is_ignored_and_the_production_bible_is_untouched(book, capsys):
    before = snapshot(book)
    bible = block("BIBLE", "B001-001-c0001", [HUT])
    client = FakeClient(bible + valid_reply(book))
    assert ktrial.cmd_scenes(args(), client=client) == 0
    out = capsys.readouterr().out
    assert len(client.calls) == 1 and "BIBLE block" in out and "ignored" in out
    assert "signal_hut" not in B.load_locations(book / "bible")["locations"]
    assert snapshot(book) == before                                          # byte-identical, nothing new anywhere


def test_a_location_only_a_bible_block_adds_is_an_error(book, capsys):
    n = counts(book)[1]
    lines = [dict(l, loc="signal_hut") if l["type"] == "group" else l for l in chapter_lines(n, 1)]
    reply = block("BIBLE", "B001-001-c0001", [HUT]) + block("IMAGES", "c0001", lines)
    before = snapshot(book)
    client = FakeClient(reply)
    assert ktrial.cmd_scenes(args(), client=client) == 1
    out = capsys.readouterr().out
    assert "location 'signal_hut' is not in the location registry" in client.calls[1].user
    assert len(client.calls) == 3 and "still not accepted after 3 attempts" in out
    assert not (trial_of(book) / "scenes").exists() and snapshot(book) == before


def test_an_unknown_cast_key_is_rejected_and_asked_again(book, capsys):
    n = counts(book)[1]
    lines = chapter_lines(n, 1)
    for l in lines:
        if l["type"] == "image" and l["n"] == 1:
            l["cast"] = [dict(l["cast"][0], key="ghost_rider")]
    client = FakeClient(block("IMAGES", "c0001", lines), valid_reply(book))
    assert ktrial.cmd_scenes(args(), client=client) == 0
    assert "unknown character 'ghost_rider'" in client.calls[1].user
    assert "ghost_rider" not in json.dumps(trial_scene(book))
    # and with a model that never fixes it: three attempts, then no file
    shutil.rmtree(trial_of(book))
    bad = FakeClient(block("IMAGES", "c0001", lines))
    assert ktrial.cmd_scenes(args(), client=bad) == 1
    assert len(bad.calls) == 3 and not (trial_of(book) / "scenes").exists()
    assert "unknown character 'ghost_rider'" in capsys.readouterr().out


def test_a_picture_with_too_many_attached_people_is_rejected_by_the_contract(book):
    n = counts(book)[1]
    lines = chapter_lines(n, 1)
    for l in lines:
        if l["type"] == "image" and l["n"] == 2:
            l["cast"] = [dict(l["cast"][0]), dict(l["cast"][1])]
    client = FakeClient(block("IMAGES", "c0001", lines))
    assert ktrial.cmd_scenes(args(max_characters=1), client=client) == 1
    assert "2 attached characters" in client.calls[1].user and "at most 1" in client.calls[1].user


def test_from_production_copies_the_file_and_calls_nobody(book, monkeypatch, capsys):
    monkeypatch.setattr(ktrial, "make_client", lambda *a, **k: pytest.fail("Gemini must not be built"))
    before = snapshot(book)
    assert ktrial.cmd_scenes(args(from_production=True)) == 0
    dest = trial_of(book) / "scenes" / "00001.json"
    assert dest.read_bytes() == S.scenes_path(ep_dir(book), 1).read_bytes()
    assert not (trial_of(book) / "gemini").exists()
    out = capsys.readouterr().out
    assert "no Gemini call" in out and f"windows: {counts(book)[1]}" in out
    assert snapshot(book) == before


def test_from_production_without_a_production_file_is_an_error(book, capsys):
    S.scenes_path(ep_dir(book), 1).unlink()
    assert ktrial.cmd_scenes(args(from_production=True)) == 1
    assert "no production scene file" in capsys.readouterr().out


def test_an_existing_trial_file_is_not_replaced_without_force(book, capsys):
    assert ktrial.cmd_scenes(args(from_production=True)) == 0
    target = trial_of(book) / "scenes" / "00001.json"
    target.write_text('{"mine": true}\n', encoding="utf-8")
    client = FakeClient(valid_reply(book))
    assert ktrial.cmd_scenes(args(), client=client) == 1                       # Gemini mode
    assert ktrial.cmd_scenes(args(from_production=True)) == 1                  # copy mode
    assert client.calls == [] and target.read_text(encoding="utf-8") == '{"mine": true}\n'
    assert "--force" in capsys.readouterr().out
    assert ktrial.cmd_scenes(args(force=True), client=client) == 0
    assert len(client.calls) == 1 and trial_scene(book)["version"] == 2


def test_the_hindi_must_be_locked(book, capsys):
    (ep_dir(book) / "hindi.lock.json").unlink()
    assert ktrial.cmd_scenes(args(), client=FakeClient(valid_reply(book))) == 1
    assert "unlocked" in capsys.readouterr().out


def test_episode_of_and_the_ep_option(book, capsys):
    assert ktrial.episode_of("tide", 1) == 1 and ktrial.episode_of("tide", 2) == 1
    with pytest.raises(ktrial.TrialError, match="chapter 99 is in no episode"):
        ktrial.episode_of("tide", 99)
    assert ktrial.cmd_scenes(args(chapter=99, from_production=True)) == 1
    assert ktrial.cmd_scenes(args(ep=7, from_production=True)) == 1
    assert "episode 7" in capsys.readouterr().out
    assert ktrial.cmd_scenes(args(ep=1, from_production=True)) == 0


def test_trial_dir_is_under_work(book):
    assert ktrial.trial_dir("tide", 24) == book / "_work" / "ktrial" / "c0024"


# ---------------------------------------------------------------- prompts

def with_scenes(book):
    assert ktrial.cmd_scenes(args(from_production=True)) == 0


def test_prompts_write_the_plan_the_prompts_and_square_references(book, capsys):
    with_scenes(book)
    before = snapshot(book)
    capsys.readouterr()
    assert ktrial.cmd_prompts(args()) == 0
    out = capsys.readouterr().out
    t = trial_of(book)
    plan = json.loads((t / "plan.json").read_text(encoding="utf-8"))
    doc = trial_scene(book)
    n = counts(book)[1]
    assert list(plan) == ["version", "book", "episode", "chapter", "width", "height", "flow_images",
                          "production_scenes", "trial_scenes", "windows", "plates", "shots"]
    assert (plan["version"], plan["book"], plan["episode"], plan["chapter"]) == (1, "tide", 1, 1)
    assert plan["width"] % kcomfy.SIZE_STEP == 0 and plan["height"] % kcomfy.SIZE_STEP == 0
    assert (plan["width"], plan["height"]) == (1376, 768)
    assert Path(plan["flow_images"]) == ep_dir(book) / "images" / "c0001" and Path(plan["flow_images"]).is_absolute()
    assert Path(plan["production_scenes"]) == S.scenes_path(ep_dir(book), 1)
    assert plan["trial_scenes"] == "scenes/00001.json"
    wins = P.chapter_windows(ep_dir(book), 1)
    assert plan["windows"] == {str(k): w for k, w in enumerate(wins, 1)} and len(plan["windows"]) == n

    chars, locs = B.load_characters(book / "bible"), B.load_locations(book / "bible")
    want = K.plan(doc, 1, chars, locs, plates=False)
    assert plan["plates"] == [] and not (t / "plates").exists()       # Qwen composes each place fresh
    assert len(plan["shots"]) == n
    for sh, w, scene in zip(plan["shots"], want["shots"], doc["scenes"]):
        tag = f"s{scene['id']:02d}"
        assert sh["id"] == f"c0001-{tag}" and sh["n"] == scene["id"] and sh["group"] == scene["group"]
        assert sh["shot"] == scene["shot"] and sh["seed"] == K.seed_for(1, tag) == w["seed"]
        assert sh["prompt"] == f"images/{tag}.prompt.txt" and sh["out"] == f"images/{tag}.jpg"
        assert (t / sh["prompt"]).read_bytes() == w["text"].encode("utf-8")
        assert all(r["role"] == "character" for r in sh["refs"]) and len(sh["refs"]) <= kcomfy.MAX_REFS
        for r in sh["refs"]:
            assert set(r) == {"role", "key", "var", "display", "file"} and r["var"] == "base"
            assert r["file"] == f"refs/{r['key']}.jpg"
        assert [r["key"] for r in sh["refs"]] == [c["key"] for c in scene["cast"]]
    # one reference per person, a square JPEG: the central square of the 1376x768 portrait (no mark, person kept)
    assert sorted(p.name for p in (t / "refs").iterdir()) == ["maren_vale.jpg", "tobias_reed.jpg"]
    for f in (t / "refs").iterdir():
        with Image.open(f) as im:
            assert im.format == "JPEG" and im.size == (768, 768)
            assert im.getpixel((384, 384))[0] > 180 and im.getpixel((384, 384))[1] < 80      # the red person
            assert im.getpixel((760, 760))[0] < 200                                           # the white mark is gone
    assert not (book / "bible" / "flow" / "maren_vale.jpg").exists()
    # printed numbers
    assert f"{n} shots, 0 plates" in out and "shots by number of references:" in out
    assert "prompt length (characters): min " in out and "mean " in out and "max " in out
    longest = out.split("longest prompts: ")[1].splitlines()[0]
    assert longest.count("c0001-s") == 3
    assert snapshot(book) == before


def test_a_variant_uses_the_variant_portrait(book, capsys):
    # the storm coat covers chapter 2 only
    assert ktrial.cmd_scenes(args(chapter=2, from_production=True)) == 0
    doc2 = trial_scene(book, 2)
    doc2["scenes"][0]["cast"] = [dict(doc2["scenes"][0]["cast"][0], key="maren_vale", var="storm_coat")]
    (trial_of(book, 2) / "scenes" / "00002.json").write_text(json.dumps(doc2), encoding="utf-8")
    capsys.readouterr()
    assert ktrial.cmd_prompts(args(chapter=2)) == 1
    out = capsys.readouterr().out
    assert "maren_vale.storm_coat-portrait.jpg" in out and "tobias_reed-portrait.jpg" not in out
    portrait(book / "bible" / "flow" / "maren_vale.storm_coat-portrait.jpg", (1000, 900))
    assert ktrial.cmd_prompts(args(chapter=2)) == 0
    plan = json.loads((trial_of(book, 2) / "plan.json").read_text(encoding="utf-8"))
    ref = plan["shots"][0]["refs"][0]
    assert ref["var"] == "storm_coat" and ref["file"] == "refs/maren_vale.storm_coat.jpg"
    with Image.open(trial_of(book, 2) / ref["file"]) as im:
        assert im.size == (900, 900)                                          # the whole short side, centred


def test_a_missing_portrait_is_an_error_that_lists_every_missing_file(book, capsys):
    with_scenes(book)
    for stem in ("maren_vale", "tobias_reed"):
        (book / "bible" / "flow" / f"{stem}-portrait.jpg").unlink()
    (book / "bible" / "flow" / "maren_vale-body.jpg").write_bytes(b"x")       # the body sheet is never used
    capsys.readouterr()
    assert ktrial.cmd_prompts(args()) == 1
    out = capsys.readouterr().out
    assert "maren_vale-portrait.jpg" in out and "tobias_reed-portrait.jpg" in out
    assert not (trial_of(book) / "plan.json").exists() and not (trial_of(book) / "refs").exists()
    assert not (trial_of(book) / "images").exists()


def test_too_many_references_is_an_error_naming_the_shot(book, capsys, monkeypatch):
    monkeypatch.setattr(kcomfy, "MAX_REFS", 2)
    with_scenes(book)
    chars = json.loads((book / "bible" / "characters.json").read_text(encoding="utf-8"))
    chars["characters"]["old_harl"]["variants"]["base"]["flow_name"] = "Old Harl"
    (book / "bible" / "characters.json").write_text(json.dumps(chars, ensure_ascii=False), encoding="utf-8")
    f = trial_of(book) / "scenes" / "00001.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    cast = doc["scenes"][1]["cast"]
    cast.append(dict(cast[0], key="old_harl", position="far right, by the boat"))
    cast.append(dict(cast[0], key="tobias_reed", position="behind"))
    f.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert ktrial.cmd_prompts(args()) == 1
    out = capsys.readouterr().out
    assert "shot c0001-s02" in out and f"limit is {kcomfy.MAX_REFS}" in out
    assert not (trial_of(book) / "plan.json").exists()


def test_prompts_need_the_trial_scene_file(book, capsys):
    assert ktrial.cmd_prompts(args()) == 1
    assert "novel ktrial scenes --chapter 1" in capsys.readouterr().out


def test_a_scene_file_of_another_hindi_is_refused(book, capsys):
    with_scenes(book)
    f = trial_of(book) / "scenes" / "00001.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["scenes"] = doc["scenes"][:-1]
    f.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert ktrial.cmd_prompts(args()) == 1
    assert "Hindi windows" in capsys.readouterr().out


def test_prompts_can_run_again(book):
    with_scenes(book)
    assert ktrial.cmd_prompts(args()) == 0
    first = (trial_of(book) / "plan.json").read_bytes()
    assert ktrial.cmd_prompts(args()) == 0
    assert (trial_of(book) / "plan.json").read_bytes() == first


# ---------------------------------------------------------------- the command

def test_the_cli_row_sits_after_images():
    from novel.cli import COMMANDS
    names = [row[0] for row in COMMANDS]
    assert names[names.index("images") + 1] == "ktrial" and names.count("ktrial") == 1
    row = COMMANDS[names.index("ktrial")]
    assert row[2] == "novel.commands.ktrial_cmd:main" and "FLUX.3 klein" in row[1]


def test_scenes_and_prompts_dispatch(book, capsys):
    before = snapshot(book)
    assert ktrial_cmd.main(["scenes", "--book", "tide", "--chapter", "1", "--from-production"]) == 0
    assert (trial_of(book) / "scenes" / "00001.json").exists()
    assert ktrial_cmd.main(["scenes", "--book", "tide", "--chapter", "1", "--from-production"]) == 1   # no --force
    assert ktrial_cmd.main(["scenes", "--book", "tide", "--chapter", "1", "--from-production", "--force"]) == 0
    assert ktrial_cmd.main(["prompts", "--book", "tide", "--chapter", "1", "--ep", "1"]) == 0
    assert (trial_of(book) / "plan.json").exists()
    assert snapshot(book) == before
    out = capsys.readouterr().out
    assert out.startswith("[tide]")


def test_main_through_the_novel_cli(book, capsys):
    from novel.cli import main
    assert main(["ktrial", "scenes", "--book", "tide", "--chapter", "1", "--from-production"]) == 0
    assert ktrial_cmd.main([]) == 0 and ktrial_cmd.main(["bogus"]) == 2
    out = capsys.readouterr().out
    assert "usage: novel ktrial scenes" in out and "unknown subcommand 'bogus'" in out
    assert "novel ktrial url --chapter N ADDRESS" in out and "[--domain HOST]" in out and "url" in ktrial_cmd.SUBCOMMANDS


def test_the_book_defaults_to_the_current_book(book, capsys):
    from novel.core import context
    context.write_state("tide")
    assert ktrial_cmd.main(["scenes", "--chapter", "1", "--from-production"]) == 0
    assert (trial_of(book) / "scenes" / "00001.json").exists()


def test_scenes_defaults(book):
    a = ktrial_cmd.build_parser("scenes").parse_args(["--chapter", "24"])
    assert (a.chapter, a.book, a.ep, a.max_characters, a.from_production, a.force) == (24, None, None, 4, False, False)
    with pytest.raises(SystemExit):
        ktrial_cmd.build_parser("scenes").parse_args([])                      # --chapter is required


def test_the_gpu_half_is_imported_lazily_and_called_with_the_trial_folder(book, monkeypatch):
    calls = []

    def fake(name):
        def cmd(a, trial):
            calls.append((name, a, trial))
            return {"up": 11, "status": 12, "down": 13, "run": 14, "report": 15, "url": 16}[name]
        return cmd
    mod = SimpleNamespace(**{f"cmd_{n}": fake(n) for n in ("up", "status", "down", "run", "report", "url")})
    import novel.stages as stages

    # not importable at all: scenes and prompts do not need it
    monkeypatch.setitem(sys.modules, "novel.stages.ktrial_run", None)
    monkeypatch.delattr(stages, "ktrial_run", raising=False)       # set when another test imported the real module
    assert ktrial_cmd.main(["scenes", "--book", "tide", "--chapter", "1", "--from-production"]) == 0
    assert ktrial_cmd.main(["prompts", "--book", "tide", "--chapter", "1"]) == 0
    with pytest.raises(ImportError):
        ktrial_cmd.main(["run", "--book", "tide", "--chapter", "1"])

    monkeypatch.setitem(sys.modules, "novel.stages.ktrial_run", mod)
    monkeypatch.setattr(stages, "ktrial_run", mod, raising=False)
    trial = ktrial.trial_dir("tide", 1)
    names = ("up", "status", "down", "run", "report")
    codes = [ktrial_cmd.main([n, "--book", "tide", "--chapter", "1"]) for n in names]
    assert codes == [11, 12, 13, 14, 15]
    assert [(c[0], c[2]) for c in calls] == [(n, trial) for n in names]
    assert all(c[1].book == "tide" and c[1].chapter == 1 for c in calls)
    # url has one positional, the address; it is dispatched the same lazy way, with the trial folder
    calls.clear()
    assert ktrial_cmd.main(["url", "--book", "tide", "--chapter", "1", "https://abc.ngrok-free.app"]) == 16
    (name, a, t), = calls
    assert (name, t) == ("url", trial) and a.address == "https://abc.ngrok-free.app" and a.chapter == 1
    assert a.book == "tide"
    with pytest.raises(SystemExit):
        ktrial_cmd.main(["url", "--book", "tide", "--chapter", "1"])                 # the address is required
    with pytest.raises(SystemExit):
        ktrial_cmd.build_parser("url").parse_args(["--chapter", "1", "a.b", "--domain", "x"])   # --domain is for up


def test_the_options_of_up_and_run(book, monkeypatch):
    seen = {}
    mod = SimpleNamespace(cmd_up=lambda a, t: seen.setdefault("up", a) and 0,
                          cmd_run=lambda a, t: seen.setdefault("run", a) and 0)
    monkeypatch.setitem(sys.modules, "novel.stages.ktrial_run", mod)
    import novel.stages as stages
    monkeypatch.setattr(stages, "ktrial_run", mod, raising=False)
    ktrial_cmd.main(["up", "--book", "tide", "--chapter", "24", "--account", "acct", "--tunnel", "cloudflared",
                     "--idle-minutes", "5", "--max-hours", "2.5", "--gpu", "0", "--comfy-arg=--fp16-unet",
                     "--comfy-arg=--lowvram", "--domain", "Some-Words.ngrok-free.dev", "--dry-run", "--no-wait"])
    a = seen["up"]
    assert a.domain == "Some-Words.ngrok-free.dev"                           # normalised later, in ktrial_run
    assert (a.account, a.tunnel, a.idle_minutes, a.max_hours, a.gpu) == ("acct", "cloudflared", 5, 2.5, 0)
    assert a.comfy_arg == ["--fp16-unet", "--lowvram"] and a.dry_run and a.no_wait and a.chapter == 24
    ktrial_cmd.main(["run", "--book", "tide", "--chapter", "1", "--only", "s05,g01", "--limit", "3", "--reroll",
                     "--redo", "--plates-only"])
    a = seen["run"]
    assert (a.only, a.limit, a.reroll, a.redo, a.plates_only) == ("s05,g01", 3, True, True, True)
    d = ktrial_cmd.build_parser("up").parse_args(["--chapter", "1"])
    assert (d.tunnel, d.idle_minutes, d.max_hours, d.gpu, d.comfy_arg, d.dry_run, d.no_wait, d.account) == (
        "ngrok", 20, 6.0, 1, [], False, False, None)
    assert d.domain is None
    with pytest.raises(SystemExit):
        ktrial_cmd.build_parser("up").parse_args(["--chapter", "1", "--tunnel", "frp"])


def test_a_whole_trial_leaves_production_alone(book):
    """scenes (Gemini, with a bible block in the reply) then prompts: only _work/ktrial changes."""
    before = snapshot(book)
    client = FakeClient(block("BIBLE", "B001-001-c0001", [HUT]) + valid_reply(book))
    assert ktrial.cmd_scenes(args(), client=client) == 0
    assert ktrial.cmd_prompts(args()) == 0
    assert snapshot(book) == before
    names = {p.relative_to(trial_of(book)).as_posix() for p in trial_of(book).rglob("*") if p.is_file()}
    assert {"scenes/00001.json", "plan.json", "gemini/001.request.txt", "gemini/001.reply.txt", "refs/maren_vale.jpg",
            "images/s01.prompt.txt"} <= names
    assert not any(n.endswith(".tmp") for n in names)


def test_a_qwen_character_sheet_is_sent_whole_instead_of_the_portrait_crop(book, capsys):
    with_scenes(book)
    sheet = book / "_work" / "ktrial" / "c0024" / "qwen-image-turbo" / "charsheet" / "maren_vale_sheet_seed202.jpg"
    sheet.parent.mkdir(parents=True)
    Image.new("RGB", (1376, 768), (0, 0, 255)).save(sheet, "JPEG")
    capsys.readouterr()
    assert ktrial.cmd_prompts(args()) == 0
    assert "1 character sheet(s)" in capsys.readouterr().out
    t = trial_of(book)
    assert (t / "refs" / "maren_vale.jpg").read_bytes() == sheet.read_bytes()       # whole, not cropped
    with Image.open(t / "refs" / "tobias_reed.jpg") as im:
        assert im.size == (768, 768)                                                 # no sheet: the portrait crop
