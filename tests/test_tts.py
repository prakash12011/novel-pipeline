"""novel tts against the fake Kaggle CLI: temp home, temp data, temp Kaggle token folder."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from novel import cli
from novel.core import context, paths
from novel.stages import episode as E
from novel.stages.manifest import Chapter, Manifest

REPO = Path(__file__).resolve().parents[1]
FAKE = REPO / "tests" / "fakes" / "fake_kaggle.py"
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not installed: the fake Kaggle builds its mp3s with ffmpeg")

# Invented text: two short chapters, a few paragraphs each.
TEXT = {
    1: "चैप्टर 1: नीली घाटी\n\nनदी के किनारे एक छोटा सा घर था। उसमें एक बूढ़ा माली रहता था।\n\n"
       "हर सुबह वह फूलों को पानी देता था। शाम को वह तारों को गिनता था।\n\nकिसी ने उसे कभी उदास नहीं देखा।",
    2: "चैप्टर 2: चाँदी की चाबी\n\nएक दिन माली को मिट्टी में एक चमकती चाबी मिली।\n\n"
       "उसने सोचा कि यह किस दरवाज़े की होगी। फिर वह हँसा और काम में लग गया।",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, data, khome, state = (tmp_path / n for n in ("home", "data", "kaggle-home", "fake-state"))
    for d in (home, data, khome, state):
        d.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({
        "data_dir": str(data),
        "tts": {"voice": "TestVoice"},
        "kaggle": {"account": "tester", "profiles": {"tester": {"username": "fakeuser"},
                                                      "other": {"username": "otheruser"}}},
    }), encoding="utf-8")
    (khome / "access_token-tester").write_text("KGAT_testtoken_111111", encoding="utf-8")
    calls = tmp_path / "calls.log"
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setenv("NOVEL_KAGGLE_HOME", str(khome))
    monkeypatch.setenv("NOVEL_KAGGLE_CMD", f'"{sys.executable}" "{FAKE}"')
    monkeypatch.setenv("FAKE_KAGGLE_STATE", str(state))
    monkeypatch.setenv("FAKE_KAGGLE_CALLS", str(calls))
    for k in ("FAKE_KAGGLE_RUNNING_POLLS", "FAKE_KAGGLE_ERROR", "FAKE_KAGGLE_HASH_MISMATCH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOVEL_TTS_UPLOAD_WAIT", "0")
    old = E.EPISODES
    make_episode("coi")
    yield SimpleNamespace(home=home, data=data, khome=khome, calls=calls, root=paths.ep_root("coi", 1))
    E.EPISODES = old
    context._ACTIVE_BOOK = None


def make_episode(book, lock=(1, 2)):
    context.activate(book)
    root = paths.ep_root(book, 1)
    (root / "hindi").mkdir(parents=True)
    chapters = []
    for n, text in TEXT.items():
        (root / "hindi" / f"{n:05d}.txt").write_text(text, encoding="utf-8")
        chapters.append(Chapter(idx=n, title=text.split("\n")[0], source=f"{n:04d}_x.txt"))
    Manifest(path=root / "manifest.json", chapters=chapters).save()
    (root / "episode.json").write_text(json.dumps({"episode": 1, "start": 1, "end": 2}))
    if lock:
        from novel.stages.episode_translate import cmd_lock       # the real lock code
        cmd_lock(SimpleNamespace(episode=1, chapters=",".join(map(str, lock))))


def novel(sub, *argv):
    return cli.main(["tts", sub, "--book", "coi", "--ep", "1", *argv])


def fake_calls(env):
    return env.calls.read_text(encoding="utf-8").splitlines() if env.calls.exists() else []


def test_dry_run_stages_and_calls_nothing(env, capsys):
    before = hashlib.sha256((REPO / "kaggle/payload/config.yaml").read_bytes()).hexdigest()
    assert novel("push", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "[coi" in out and "dry run" in out and "fakeuser/coi-e001-tts" in out
    ds = paths.kaggle_work_dir("coi") / "coi-e001-tts" / "dataset"
    assert (ds / "abpipe" / "runner.py").is_file()
    cfg = yaml.safe_load((ds / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["tts"]["maya2"]["voice"] == "TestVoice"
    assert cfg["tts"]["maya2"]["model_id"] == "maya-research/maya2-native"
    assert cfg["tts"]["backend"] == "indicf5"       # the rest of the file is as it was
    assert (ds / "hindi" / "00001.txt").is_file() and (ds / "hindi" / "00002.txt").is_file()
    assert hashlib.sha256((REPO / "kaggle/payload/config.yaml").read_bytes()).hexdigest() == before
    assert fake_calls(env) == []
    assert not (env.root / "tts-state.json").exists()
    assert not (env.khome / "access_token").exists()


@needs_ffmpeg
def test_push_then_watch_pulls_verified_audio(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "2")
    assert novel("push") == 0
    assert (env.khome / "access_token").read_text() == "KGAT_testtoken_111111"
    assert novel("watch", "--interval-seconds", "0.1") == 0
    out = capsys.readouterr().out
    assert out.count("RUNNING") == 2 and "COMPLETE" in out and "2/2 chapters brought home" in out
    for n in (1, 2):
        assert (env.root / "audio" / f"{n:05d}.mp3").stat().st_size > 1000
        m = json.loads((env.root / "timing" / f"{n:05d}.json").read_text(encoding="utf-8"))
        assert m["measured"] and m["chunks"]
    man = Manifest.load(env.root / "manifest.json")
    assert [c.status for c in man.chapters] == ["done", "done"]
    assert all(c.audio and c.timing and c.seconds for c in man.chapters)
    assert all(l.endswith("...111111") for l in fake_calls(env))     # only the fake, with the token
    # chapters that now have audio are not offered again
    assert novel("push") == 0
    assert "nothing to narrate" in capsys.readouterr().out


@needs_ffmpeg
def test_second_push_before_pull_is_refused(env):
    assert novel("push") == 0
    with pytest.raises(SystemExit) as e:
        novel("push")
    assert "has not been pulled" in str(e.value)


@needs_ffmpeg
def test_hash_mismatch_is_not_taken(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_HASH_MISMATCH", "1")
    assert novel("push") == 0
    assert novel("pull") == 1
    out = capsys.readouterr().out
    assert out.count("NOT TAKEN: narrated text is not the locked text") == 2
    assert not list((env.root / "audio").glob("*.mp3"))


@needs_ffmpeg
def test_kernel_error_makes_watch_exit_1_with_log_lines(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_ERROR", "Traceback (most recent call last):|CUDA out of memory")
    assert novel("push") == 0
    assert novel("watch", "--interval-seconds", "0.1") == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "CUDA out of memory" in out
    assert not list((env.root / "audio").glob("*.mp3")) if (env.root / "audio").exists() else True


def test_missing_token_names_the_expected_file(env):
    (env.khome / "access_token-tester").unlink()
    with pytest.raises(SystemExit) as e:
        novel("push")
    assert str(env.khome / "access_token-tester") in str(e.value)
    assert fake_calls(env) == []


def test_account_option_and_unknown_profile(env):
    with pytest.raises(SystemExit) as e:
        novel("push", "--account", "ghost")
    assert "unknown Kaggle profile 'ghost'" in str(e.value)
    with pytest.raises(SystemExit) as e:
        novel("push", "--account", "other")              # known profile, no token file
    assert "access_token-other" in str(e.value)


def test_only_locked_chapters_by_default(env, capsys):
    shutil.rmtree(env.root)
    make_episode("coi", lock=(1,))
    novel("push", "--dry-run")
    ds = paths.kaggle_work_dir("coi") / "coi-e001-tts" / "dataset" / "hindi"
    assert [p.name for p in ds.iterdir()] == ["00001.txt"]


def test_coi_slugs_match_the_old_format(env):
    from novel.stages import episode_tts as T
    for n in (1, 2, 10):
        assert T.slug(n) == f"coi-e{n:03d}-tts"            # old: f"coi-e{episode:03d}-tts"
        assert T.kernel_slug(n) == f"coi-e{n:03d}-narrate"  # old: f"coi-e{episode:03d}-narrate"
    context._ACTIVE_BOOK = "lotm"
    assert T.slug(7) == "lotm-e007-tts" and T.kernel_slug(7) == "lotm-e007-narrate"


def test_kaggle_cmd_order(monkeypatch):
    from novel.stages import kbatch
    monkeypatch.setenv("NOVEL_KAGGLE_CMD", 'py "D:/x y/fake.py"')
    assert kbatch.kaggle_cmd() == ["py", "D:/x y/fake.py"]
    monkeypatch.delenv("NOVEL_KAGGLE_CMD")
    monkeypatch.setattr(shutil, "which", lambda n: "C:/bin/kaggle.exe")
    assert kbatch.kaggle_cmd() == ["C:/bin/kaggle.exe"]
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert kbatch.kaggle_cmd() == [sys.executable, "-m", "kaggle"]


# ---------------------------------------------------------------- novel tts reset (a run that ended in ERROR)

def state_of(env):
    return json.loads((env.root / "tts-state.json").read_text(encoding="utf-8"))


def errored_run(env, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_ERROR", "Traceback (most recent call last):|CUDA out of memory")
    assert novel("push") == 0
    return state_of(env)


def test_error_run_blocks_push_and_pull_until_reset(env, capsys, monkeypatch):
    errored_run(env, monkeypatch)
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        novel("pull")
    assert "not finished" in str(e.value) and "novel tts reset" in str(e.value)
    with pytest.raises(SystemExit) as e:
        novel("push")
    assert "has not been pulled" in str(e.value) and "novel tts reset" in str(e.value)


def test_watch_and_status_name_reset_when_the_run_ended_in_error(env, capsys, monkeypatch):
    errored_run(env, monkeypatch)
    assert novel("watch", "--interval-seconds", "0.1") == 1
    assert "novel tts reset" in capsys.readouterr().out
    assert state_of(env)["last_status"] == "ERROR"
    assert novel("status") == 0
    assert "novel tts reset" in capsys.readouterr().out
    # `novel status` reads the note (no Kaggle call) and names the command
    cli.main(["status", "--book", "coi", "--ep", "1"])            # exit code says NOT READY: not the point here
    out = capsys.readouterr().out
    assert "FAILED" in out and "novel tts reset" in out and "RUNNING" not in out


def test_reset_abandons_an_errored_run_and_push_works_again(env, capsys, monkeypatch):
    first = errored_run(env, monkeypatch)
    audio = env.root / "audio"
    audio.mkdir()
    (audio / "00001.mp3").write_bytes(b"already pulled audio")      # must never be touched
    capsys.readouterr()
    assert novel("reset", "--yes") == 0
    out = capsys.readouterr().out
    assert "Kaggle says now: ERROR" in out and "chapters 1, 2" in out and "Abandoned" in out
    st = state_of(env)
    assert st["abandoned"] and st["abandoned_status"] == "ERROR" and st["pushed"] == first["pushed"]
    assert len(st["history"]) == 1 and st["history"][0]["pushed"] == first["pushed"] and "history" not in st["history"][0]
    assert (audio / "00001.mp3").read_bytes() == b"already pulled audio"
    with pytest.raises(SystemExit) as e:
        novel("pull")
    assert "abandoned" in str(e.value)
    monkeypatch.delenv("FAKE_KAGGLE_ERROR")
    assert novel("push") == 0                                    # push is free again
    st2 = state_of(env)
    assert st2["pulled"] is False and "abandoned" not in st2 and len(st2["history"]) == 1
    assert novel("reset", "--yes") == 1                           # the new run is still going/complete: refused
    assert state_of(env) == st2


def test_reset_asks_for_confirmation(env, capsys, monkeypatch):
    errored_run(env, monkeypatch)
    from novel.stages import episode_tts as T
    before = state_of(env)
    asked = []

    def args():
        return SimpleNamespace(episode=1, yes=False)
    assert T.cmd_reset(args(), ask=lambda p: asked.append(p) or "n") == 1
    assert len(asked) == 1 and state_of(env) == before
    assert T.cmd_reset(args(), ask=lambda p: (_ for _ in ()).throw(EOFError)) == 1       # no keyboard: no change
    assert state_of(env) == before
    assert T.cmd_reset(args(), ask=lambda p: "y") == 0
    assert state_of(env)["abandoned"]


def test_reset_refuses_a_running_kernel(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "50")
    assert novel("push") == 0
    before = state_of(env)
    capsys.readouterr()
    assert novel("reset", "--yes") == 1
    out = capsys.readouterr().out
    assert "RUNNING" in out and "novel tts watch" in out
    assert state_of(env) == before


def test_reset_refuses_when_output_is_waiting(env, capsys):
    assert novel("push") == 0                           # the fake finishes at once: COMPLETE
    before = state_of(env)
    capsys.readouterr()
    assert novel("reset", "--yes") == 1
    out = capsys.readouterr().out
    assert "novel tts pull" in out and "REFUSED" in out
    assert state_of(env) == before


def test_reset_with_nothing_recorded(env, capsys):
    assert novel("reset", "--yes") == 0
    assert "nothing to reset" in capsys.readouterr().out
    assert not (env.root / "tts-state.json").exists()
