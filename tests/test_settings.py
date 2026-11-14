import pytest
import yaml

from novel.core import paths, settings


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(h))
    (h / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    return h


def w(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj), encoding="utf-8")


def test_all_five_layers_precedence(home):
    d = paths.data_dir()
    w(home / "settings.yaml", {"data_dir": str(d), "images": {"max_seconds": 6.0}})
    w(d / "demo" / "book.yaml", {"images": {"max_seconds": 5.0}})
    w(d / "demo" / "episodes" / "E003" / "episode.yaml",
      {"images": {"max_seconds": 4.0}, "chapters": {25: {"images": {"max_seconds": 3.0}}}})
    assert settings.get("images.max_seconds") == 6.0
    assert settings.get("images.max_seconds", book="demo") == 5.0
    assert settings.get("images.max_seconds", book="demo", ep=3) == 4.0
    assert settings.get("images.max_seconds", book="demo", ep=3, ch=25) == 3.0
    assert settings.get("images.max_seconds", book="demo", ep=3, ch=26) == 4.0
    assert settings.get("images.paced_seconds", book="demo", ep=3, ch=25) == 7.0


def test_explain_layer_names(home):
    d = paths.data_dir()
    w(d / "demo" / "book.yaml", {"tts": {"voice": "Z"}})
    w(d / "demo" / "episodes" / "E003" / "episode.yaml",
      {"chapters": {25: {"video": {"captions": {"size": 50}}}}})
    ex = settings.explain("demo", 3, 25)
    assert ex["tts.engine"] == ("maya2", "defaults")
    assert ex["tts.voice"] == ("Z", "book:demo")
    assert ex["video.captions.size"] == (50, "chapter:25")
    assert ex["data_dir"][1] == "home"
    ep = settings.explain("demo", 3)
    assert ep["video.captions.size"] == (38, "defaults")


def test_lists_replace_dicts_merge(home):
    d = paths.data_dir()
    w(d / "demo" / "book.yaml", {"publish": {"tags": ["a", "b"], "category": "Music"},
                                 "video": {"logo": {"enabled": True}}})
    w(d / "demo" / "episodes" / "E001" / "episode.yaml", {"publish": {"tags": ["c"]}})
    eff = settings.effective("demo", 1)
    assert eff["publish"]["tags"] == ["c"]
    assert eff["publish"]["category"] == "Music"
    logo = eff["video"]["logo"]                  # book sets enabled; the other keys merge in from defaults
    assert logo["enabled"] is True
    assert (logo["file"], logo["position"], logo["margin"], logo["opacity"]) == ("assets/logo.png", "top-left", 26, 1.0)


def test_chapter_scope_error_names_key(home):
    d = paths.data_dir()
    w(d / "demo" / "episodes" / "E001" / "episode.yaml",
      {"chapters": {5: {"tts": {"voice": "X"}}}})
    with pytest.raises(SystemExit) as e:
        settings.effective("demo", 1, 5)
    assert "tts.voice" in str(e.value)


def test_missing_files_skipped(home):
    eff = settings.effective("nobook", 9, 9)
    assert eff["channel"]["name"] == "Manga and Novel"
    assert [n for n, _ in settings.layers("nobook", 9, 9)][:2] == ["defaults", "home"]


def test_defaults_match_pacing_constants():
    from novel.stages import episode_scenes as s
    assert settings.get("images.paced_seconds") == s.PACED_SECONDS
    assert settings.get("images.max_seconds") == s.MAX_SECONDS
    assert settings.get("images.chars_per_second") == s.CHARS_PER_SECOND


def test_asset_path():
    p = settings.asset_path("assets/fonts/Anton-Regular.ttf")
    assert p == paths.repo_root() / "assets" / "fonts" / "Anton-Regular.ttf" and p.exists()
    assert str(settings.asset_path("C:/x/y.png")).endswith("y.png")


def test_paths_layout(home):
    d = paths.data_dir()
    assert paths.ep_root("demo", 7) == d / "demo" / "episodes" / "E007"
    assert paths.kaggle_work_dir("demo") == d / "demo" / "_work" / "kaggle"
    assert paths.fonts_dir().is_dir()


def test_paths_errors_name_fix_command(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_HOME", str(tmp_path / "empty"))
    with pytest.raises(SystemExit) as e:
        paths.data_dir()
    assert "novel settings init --data-dir" in str(e.value)
    h = tmp_path / "h2"
    h.mkdir()
    (h / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(tmp_path / "gone")}))
    monkeypatch.setenv("NOVEL_HOME", str(h))
    with pytest.raises(SystemExit) as e:
        paths.data_dir()
    assert "novel settings init --data-dir" in str(e.value)
