import json

import pytest
import yaml

from novel.core import context, paths, secrets


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(h))
    (h / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    return h


def test_state_roundtrip_and_resolve(home):
    with pytest.raises(SystemExit) as e:
        context.resolve()
    assert "novel use" in str(e.value)
    context.write_state("coi", 3)
    assert json.loads((home / "state.json").read_text()) == {"book": "coi", "episode": 3}
    assert context.resolve() == ("coi", 3)
    assert context.resolve(ep=5) == ("coi", 5)
    assert context.resolve("other") == ("other", None)
    assert context.resolve("other", 2) == ("other", 2)


def test_header(home):
    ep = paths.ep_root("coi", 7)
    ep.mkdir(parents=True)
    assert context.header("coi", 7) == "[coi \u00b7 E007]"
    (ep / "episode.json").write_text(json.dumps({"start": 61, "end": 70}))
    assert context.header("coi", 7) == "[coi \u00b7 E007 \u00b7 ch 61-70]"


def test_activate_points_stages_at_data(home):
    from novel.stages import episode
    old = episode.EPISODES
    try:
        context.activate("demo")
        assert episode.root(7) == paths.data_dir() / "demo" / "episodes" / "E007"
        assert episode.root(7).is_absolute()
    finally:
        episode.EPISODES = old
        context._ACTIVE_BOOK = None


def test_gemini_keys_order_and_empty(home):
    (home / "secrets.env").write_text(
        "# c\nGEMINI_API_KEY3=c3\nGEMINI_API_KEY=a1\nGEMINI_API_KEY2=\nOTHER=x\n"
        "GEMINI_API_KEY10 = 'k10'\n", encoding="utf-8")
    assert secrets.gemini_keys() == ["a1", "k10", "c3"]
    assert secrets.load()["OTHER"] == "x"


def test_no_secrets_file(home):
    assert secrets.gemini_keys() == []
    from novel.stages import episode_translate
    with pytest.raises(SystemExit) as e:
        episode_translate.api_keys()
    assert "secrets.env" in str(e.value)
