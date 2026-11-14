import pytest
import yaml

from novel.commands import new_cmd
from novel.core import context, paths, settings

SUBS = ["source", "chapters", "bible", "gemini", "inbox", "research", "extras", "_work", "episodes"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({
        "data_dir": str(data),
        "kaggle": {"account": "acc1", "profiles": {"acc1": {"username": "u1"}, "acc2": {"username": "u2"}}},
    }), encoding="utf-8")
    monkeypatch.setattr(context, "_ACTIVE_BOOK", None)
    return data


def script(monkeypatch, answers):
    answers = list(answers)

    def fake(prompt):
        if not answers:
            raise EOFError
        a = answers.pop(0)
        if a == "^C":
            raise KeyboardInterrupt
        return a
    monkeypatch.setattr(new_cmd, "read_line", fake)


def test_interactive_creates_tree_and_book_yaml(env, monkeypatch, capsys):
    script(monkeypatch, ["The Great Test", "", "Ann Author", "", "7", "", "Nano Banana 2", "Priya", "acc2", "", ""])
    assert new_cmd.main([]) == 0
    book = env / "gt"
    for s in SUBS:
        assert (book / s).is_dir()
    eff = settings.effective(book="gt")
    assert eff["book"] == {"title": "The Great Test", "author": "Ann Author", "slug": "gt",
                           "series_label": "The Great Test", "chapters_per_episode": 7, "volumes": []}
    assert eff["images"]["flow_model"] == "Nano Banana 2"
    assert eff["tts"]["voice"] == "Priya"
    assert eff["tts"]["kaggle_profile"] == "acc2"
    assert eff["images"]["flow_project"] is None
    assert eff["video"]["disclaimer"]["text"] is None
    out = capsys.readouterr().out
    assert f"1. Copy the EPUB file(s) into {book / 'source'}" in out
    assert "2. novel extract" in out and "3. novel episode add" in out
    assert context.read_state()["book"] == "gt"


def test_defaults_are_not_written(env, monkeypatch):
    script(monkeypatch, ["Solo", "", "", "", "", "", "", "", "", "", ""])
    assert new_cmd.main([]) == 0
    data = yaml.safe_load((env / "solo" / "book.yaml").read_text(encoding="utf-8"))
    assert data["book"]["slug"] == "solo"
    assert data["book"]["chapters_per_episode"] == 10
    assert "flow_model" not in (data.get("images") or {})
    assert "voice" not in (data.get("tts") or {})
    assert "video" not in data and "flow_project" not in (data.get("images") or {})
    text = (env / "solo" / "book.yaml").read_text(encoding="utf-8")
    for key in ("# book:", "volumes", "chapter_regex", "logo", "subscribe", "publish"):
        assert key in text


def test_slug_validation_collision_and_reask(env, monkeypatch, capsys):
    (env / "taken").mkdir()
    script(monkeypatch, ["Taken", "Bad Slug!", "taken", "good-1", "", "", "", "", "", "", "", "", ""])
    assert new_cmd.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("Not valid") == 2
    assert "already exists" in out
    assert (env / "good-1" / "book.yaml").exists()


def test_default_slug_is_initials(env):
    assert new_cmd.default_slug("Circle of Inevitability", env) == "ci"
    assert new_cmd.default_slug("Reverend Insanity", env) == "ri"
    assert new_cmd.default_slug("Dune", env) == "dune"
    (env / "dune").mkdir()
    assert new_cmd.default_slug("Dune", env) == "dune-2"


def test_bad_url_reasks(env, monkeypatch, capsys):
    script(monkeypatch, ["T", "", "", "", "", "https://example.com/nothing", "https://flow.google.com/project/xyz",
                         "", "", "", "", ""])
    assert new_cmd.main([]) == 0
    assert "Not valid" in capsys.readouterr().out
    assert settings.effective(book="t")["images"]["flow_project"] == "https://flow.google.com/project/xyz"


def test_chapters_per_episode_reasks(env, monkeypatch, capsys):
    script(monkeypatch, ["T", "", "", "", "abc", "99", "12", "", "", "", "", "", ""])
    assert new_cmd.main([]) == 0
    assert capsys.readouterr().out.count("Not valid") == 2
    assert settings.effective(book="t")["book"]["chapters_per_episode"] == 12


def test_yes_with_flags(env, capsys):
    rc = new_cmd.main(["--yes", "--title", "Flag Book", "--slug", "fb", "--chapters-per-episode", "4",
                       "--author", "Zed", "--voice", "Kavya", "--disclaimer-text", "Custom words."])
    assert rc == 0
    eff = settings.effective(book="fb")
    assert eff["book"]["chapters_per_episode"] == 4 and eff["book"]["author"] == "Zed"
    assert eff["tts"]["voice"] == "Kavya"
    assert eff["video"]["disclaimer"]["text"] == "Custom words."
    assert eff["tts"]["kaggle_profile"] == "acc1"      # default = kaggle.account
    assert context.read_state()["book"] == "fb"


def test_slug_positional_argument(env):
    assert new_cmd.main(["--yes", "--title", "Whatever", "myslug"]) == 0
    assert (env / "myslug" / "book.yaml").exists()


def test_yes_errors_exit_2_and_create_nothing(env, capsys):
    with pytest.raises(SystemExit) as e:
        new_cmd.main(["--yes"])
    assert e.value.code == 2
    assert "--title" in capsys.readouterr().err
    (env / "dup").mkdir()
    with pytest.raises(SystemExit) as e:
        new_cmd.main(["--yes", "--title", "X", "--slug", "dup"])
    assert e.value.code == 2
    assert "already exists" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        new_cmd.main(["--yes", "--title", "X", "--flow-project", "nope"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        new_cmd.main(["--yes", "--title", "X", "--chapters-per-episode", "0"])
    assert e.value.code == 2
    assert sorted(p.name for p in env.iterdir()) == ["dup"]


def test_volumes_hint_has_the_right_shape(env):
    assert new_cmd.main(["--yes", "--title", "Vol Book", "--slug", "volbook"]) == 0
    text = (env / "volbook" / "book.yaml").read_text(encoding="utf-8")
    assert "volumes: [[1, 120, 1], [121, 300, 2]]" in text


def test_no_at_confirm_creates_nothing(env, monkeypatch, capsys):
    script(monkeypatch, ["T", "", "", "", "", "", "", "", "", "", "n"])
    assert new_cmd.main([]) == 1
    assert list(env.iterdir()) == []
    assert not paths.state_file().exists()
    assert "Nothing was created" in capsys.readouterr().out


def test_ctrl_c_creates_nothing(env, monkeypatch):
    script(monkeypatch, ["T", "^C"])
    assert new_cmd.main([]) == 1
    assert list(env.iterdir()) == []


def test_custom_disclaimer_when_standard_declined(env, monkeypatch):
    script(monkeypatch, ["T", "", "", "", "", "", "", "", "", "n", "My own text", ""])
    assert new_cmd.main([]) == 0
    assert settings.effective(book="t")["video"]["disclaimer"]["text"] == "My own text"


def test_unknown_kaggle_profile_reasks(env, monkeypatch, capsys):
    script(monkeypatch, ["T", "", "", "", "", "", "", "", "nope", "acc2", "", ""])
    assert new_cmd.main([]) == 0
    assert "Available: acc1, acc2" in capsys.readouterr().out
    assert settings.effective(book="t")["tts"]["kaggle_profile"] == "acc2"
