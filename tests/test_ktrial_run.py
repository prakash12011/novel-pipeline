"""novel.stages.ktrial_run: up/url/status/down against the fake Kaggle CLI, run/report against the fake ComfyUI.

The trial folder is built by hand in tmp_path (no other ktrial module is needed): four windows, two places, a few
references, two Flow pictures. No real Kaggle, tunnel, Hugging Face, ComfyUI or OpenRouter is ever called."""

import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image, ImageChops, ImageStat

from novel import kcomfy, korouter
from novel.core import context
from novel.stages import ktrial_run as K

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_comfy import FakeComfy, make_jpeg  # noqa: E402
from fake_openrouter import FakeOpenRouter  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FAKE = REPO / "tests" / "fakes" / "fake_kaggle.py"
W, H = 320, 192


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def env(tmp_path, monkeypatch):
    home, data, khome, state = (tmp_path / n for n in ("home", "data", "kaggle-home", "fake-state"))
    for d in (home, data, khome, state):
        d.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({
        "data_dir": str(data),
        "kaggle": {"account": "tester", "profiles": {"tester": {"username": "fakeuser"}}},
    }), encoding="utf-8")
    (khome / "access_token-tester").write_text("KGAT_testtoken_111111", encoding="utf-8")
    calls = tmp_path / "calls.log"
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setenv("NOVEL_KAGGLE_HOME", str(khome))
    monkeypatch.setenv("NOVEL_KAGGLE_CMD", f'"{sys.executable}" "{FAKE}"')
    monkeypatch.setenv("FAKE_KAGGLE_STATE", str(state))
    monkeypatch.setenv("FAKE_KAGGLE_CALLS", str(calls))
    for k in ("FAKE_KAGGLE_RUNNING_POLLS", "FAKE_KAGGLE_ERROR", "FAKE_KAGGLE_EXTRA_LOG",
              "FAKE_KAGGLE_EMPTY_LOG_WHILE_RUNNING", "NOVEL_NGROK_DOMAIN", "NOVEL_KTRIAL_URL_OVERRIDE",
              "NOVEL_KTRIAL_WAIT_MINUTES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOVEL_TTS_UPLOAD_WAIT", "0")
    monkeypatch.setenv("NOVEL_KTRIAL_POLL", "0")
    context.activate("coi")
    yield SimpleNamespace(calls=calls, state=state, khome=khome)
    context._ACTIVE_BOOK = None


@pytest.fixture
def fc():
    with FakeComfy(pending_polls=0) as f:
        yield f


def jpg(path: Path, w=64, h=64, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_jpeg(w, h, seed))


def make_trial(tmp_path, width=W, height=H, prompt_s02="Two people <ref_image_0> & <ref_image_1> <script>x</script>"):
    trial = tmp_path / "trial"
    (trial / "plates").mkdir(parents=True)
    (trial / "images").mkdir()
    jpg(trial / "refs" / "lumian_lee.jpg", seed=1)
    jpg(trial / "refs" / "aurore_lee.jpg", seed=2)
    flow = tmp_path / "flow"
    jpg(flow / "s01-2k.jpg", 688, 384, seed=3)
    jpg(flow / "s03-2k.jpg", 688, 384, seed=4)

    def chars(*keys):
        return {"cast": [{"key": k} for k in keys], "characters": list(keys)}

    (trial / "scenes").mkdir()
    (trial / "scenes" / "00024.json").write_text(json.dumps({"groups": {}, "scenes": [
        {"id": 1, "group": "g01", "shot": "medium", "action": "a", "focus": [0.2, 0.5], **chars("lumian_lee")},
        {"id": 2, "group": "g01", "shot": "medium", "action": "b", "focus": [0.5, 0.5],
         **chars("lumian_lee", "aurore_lee")},
        {"id": 3, "group": "g02", "shot": "establishing", "action": "c", **chars()},
        {"id": 4, "group": "g02", "shot": "close-up", "action": "d", "focus": [1, 1], **chars("aurore_lee")},
    ]}), encoding="utf-8")
    prod = tmp_path / "prod-scenes.json"
    prod.write_text(json.dumps({"scenes": [
        {"id": 1, "shot": "medium", "focus": [0.9, 0.5], **chars("a", "b")},
        {"id": 2, "shot": "medium", **chars("a")},
        {"id": 3, "shot": "close-up", "focus": [0.1, 0.1], **chars("a")},
        {"id": 4, "shot": "wide", **chars()},
    ]}), encoding="utf-8")

    def refs(*items):
        out = []
        for it in items:
            if it.startswith("g"):
                out.append({"role": "place", "group": it, "file": f"plates/{it}.jpg"})
            else:
                out.append({"role": "character", "key": it, "var": "base", "display": it.replace("_", " ").title(),
                            "file": f"refs/{it}.jpg"})
        return out

    prompts = {"s01": "One person <ref_image_0> in <ref_image_1>", "s02": prompt_s02,
               "s03": "An empty place <ref_image_0>", "s04": "Close up <ref_image_0> <ref_image_1>"}
    shots = []
    for n, (g, rs) in enumerate([("g01", ["lumian_lee", "g01"]), ("g01", ["lumian_lee", "aurore_lee", "g01"]),
                                 ("g02", ["g02"]), ("g02", ["aurore_lee", "g02"])], 1):
        (trial / "images" / f"s{n:02d}.prompt.txt").write_text(prompts[f"s{n:02d}"], encoding="utf-8")
        shots.append({"id": f"c0024-s{n:02d}", "n": n, "group": g, "shot": "medium",
                      "prompt": f"images/s{n:02d}.prompt.txt", "out": f"images/s{n:02d}.jpg", "seed": 100 + n,
                      "refs": refs(*rs)})
    plates = []
    for g, seed in (("g01", 11), ("g02", 22)):
        (trial / "plates" / f"{g}.prompt.txt").write_text(f"Place {g}, empty.", encoding="utf-8")
        plates.append({"group": g, "loc": "ruins", "name": f"Place {g}", "time": "night", "light": "moon",
                       "prompt": f"plates/{g}.prompt.txt", "out": f"plates/{g}.jpg", "seed": seed})
    (trial / "plan.json").write_text(json.dumps({
        "version": 1, "book": "coi", "episode": 3, "chapter": 24, "width": width, "height": height,
        "flow_images": str(flow), "production_scenes": str(prod), "trial_scenes": "scenes/00024.json",
        "windows": {"1": "विंडो एक", "2": "विंडो दो", "3": "विंडो तीन", "4": "विंडो चार"},
        "plates": plates, "shots": shots}, ensure_ascii=False), encoding="utf-8")
    return trial


def write_server(trial, fc, **extra):
    (trial / "server.json").write_text(json.dumps({
        "account": "tester", "user": "fakeuser", "kernel": "fakeuser/coi-ktrial-comfy", "comfy_user": "ktrial",
        "password": "pw", "tunnel": "ngrok", "pushed": "20261116T000000", "url": fc.url, **extra}), encoding="utf-8")


def ns(**kw):
    base = dict(book="coi", chapter=24, account=None, tunnel="ngrok", idle_minutes=20, max_hours=6, gpu=1,
                comfy_arg=None, domain=None, dry_run=False, no_wait=False, only=None, limit=None, reroll=False, redo=False,
                plates_only=False)
    base.update(kw)
    return SimpleNamespace(**base)


def graphs(fc):
    return list(fc.prompts.values())


def node(graph, cls):
    return next(n["inputs"] for n in graph["graph"].values() if n["class_type"] == cls)


def seed_of(g):
    return node(g, "KSampler")["seed"]


def text_of(g):
    return g["graph"]["4"]["inputs"]["text"]


def load_refs(g):
    return [n["inputs"]["image"] for k, n in sorted(g["graph"].items()) if n["class_type"] == "LoadImage"]


def fake_calls(env):
    return env.calls.read_text(encoding="utf-8").splitlines() if env.calls.exists() else []


def read(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- up / status / down

def test_up_dry_run_stages_and_calls_nothing(env, tmp_path, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_up(ns(dry_run=True, comfy_arg=["--fp16-unet"]), trial) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "fakeuser/coi-ktrial" in out
    ds = trial / "kaggle" / "dataset"
    names = sorted(p.name for p in ds.iterdir())
    marker = [n for n in names if n.startswith("push-") and n.endswith(".txt")]
    assert len(marker) == 1 and {"kcomfy.py", "session.json", "dataset-metadata.json"} <= set(names)
    assert (ds / "kcomfy.py").read_bytes() == (REPO / "novel" / "kcomfy.py").read_bytes()
    session = read(ds / "session.json")
    assert session["user"] == "ktrial" and len(session["password"]) >= 24 and session["tunnel"] == "ngrok"
    assert session["idle_minutes"] == 20 and session["max_hours"] == 6 and session["gpu"] == 1
    assert session["comfy_args"] == ["--fp16-unet"] and "domain" not in session
    assert read(ds / "dataset-metadata.json")["id"] == "fakeuser/coi-ktrial"
    kd = trial / "kaggle" / "kernel"
    nb = kd / "coi-ktrial-comfy.ipynb"
    assert nb.exists() and "@DATASET@" not in nb.read_text(encoding="utf-8")
    assert "coi-ktrial" in nb.read_text(encoding="utf-8")
    meta = read(kd / "kernel-metadata.json")
    assert meta["id"] == "fakeuser/coi-ktrial-comfy" and meta["is_private"] and meta["enable_gpu"]
    assert meta["enable_internet"] and meta["machine_shape"] == "NvidiaTeslaT4"
    assert meta["dataset_sources"] == ["fakeuser/coi-ktrial", "fakeuser/hf-token", "fakeuser/ngrok-token"]
    assert fake_calls(env) == [] and not (trial / "server.json").exists()


def test_up_without_comfy_arg_and_cloudflared_has_no_ngrok_token(env, tmp_path):
    trial = make_trial(tmp_path)
    assert K.cmd_up(ns(dry_run=True, tunnel="cloudflared"), trial) == 0
    session = read(trial / "kaggle" / "dataset" / "session.json")
    assert session["tunnel"] == "cloudflared" and "comfy_args" not in session and "domain" not in session
    meta = read(trial / "kaggle" / "kernel" / "kernel-metadata.json")
    assert meta["dataset_sources"] == ["fakeuser/coi-ktrial", "fakeuser/hf-token"]


KERNEL_PAGE = "https://www.kaggle.com/code/fakeuser/coi-ktrial-comfy"


def kernel_calls(env, what):
    return [c for c in fake_calls(env) if c.startswith(f"kernels {what} ")]


@pytest.fixture
def dead():
    """The address of a server that never answers properly (every request gets a 503): fast to fail on any OS, where
    a refused connection on Windows takes seconds."""
    with FakeComfy(fail_first=10**9) as f:
        yield f.url


# ---- the ngrok dev domain: where it comes from

@pytest.mark.parametrize("given, want", [
    ("something-words.ngrok-free.dev", "something-words.ngrok-free.dev"),
    ("  https://Some-Words.ngrok-free.dev/ ", "some-words.ngrok-free.dev"),
    ("http://a1.b2.dev//", "a1.b2.dev"),
    ("localhost", "localhost"),
])
def test_normalise_host(given, want):
    assert K.normalise_host(given) == want


@pytest.mark.parametrize("bad", ["", "   ", "two words", "a.b:8080", "https://a.b/path", "a_b.dev", "-x.dev",
                                 "x-.dev", ".x.dev", "a..b", "https://", "a.b?x=1", "ünï.dev"])
def test_normalise_host_rejects_what_is_not_a_plain_host(bad):
    with pytest.raises(SystemExit, match="not a plain host name"):
        K.normalise_host(bad, "--domain")


def test_domain_order_is_option_then_env_then_file(env, monkeypatch, capsys):
    assert K.ngrok_domain(ns()) is None                                  # nothing set, no file
    assert not K.domain_file().exists()
    (env.khome / "ngrok-domain").write_text("from-file.ngrok-free.dev\n", encoding="utf-8")
    assert K.ngrok_domain(ns()) == "from-file.ngrok-free.dev"
    monkeypatch.setenv("NOVEL_NGROK_DOMAIN", " https://From-Env.ngrok-free.dev/ ")
    assert K.ngrok_domain(ns()) == "from-env.ngrok-free.dev"
    assert (env.khome / "ngrok-domain").read_text(encoding="utf-8") == "from-file.ngrok-free.dev\n"   # env is not saved
    capsys.readouterr()
    assert K.ngrok_domain(ns(domain="From-Arg.ngrok-free.dev")) == "from-arg.ngrok-free.dev"
    assert (env.khome / "ngrok-domain").read_text(encoding="utf-8") == "from-arg.ngrok-free.dev\n"
    out = capsys.readouterr().out
    assert out.count("saved") == 1 and "from-arg.ngrok-free.dev" in out          # one line
    assert K.domain_file() == env.khome / "ngrok-domain"


def test_a_bad_domain_from_any_source_stops_before_anything_is_called(env, tmp_path, monkeypatch):
    trial = make_trial(tmp_path)
    with pytest.raises(SystemExit, match="--domain"):
        K.cmd_up(ns(domain="not a host"), trial)
    assert not K.domain_file().exists()                                  # a rejected value is never saved
    monkeypatch.setenv("NOVEL_NGROK_DOMAIN", "a.b:99")
    with pytest.raises(SystemExit, match="NOVEL_NGROK_DOMAIN"):
        K.cmd_up(ns(), trial)
    monkeypatch.delenv("NOVEL_NGROK_DOMAIN")
    K.domain_file().write_text("bad/host\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="ngrok-domain"):
        K.cmd_up(ns(), trial)
    assert fake_calls(env) == [] and not (trial / "server.json").exists() and not (trial / "kaggle").exists()


def test_session_json_has_the_domain_only_when_one_is_set(env, tmp_path, monkeypatch):
    trial = make_trial(tmp_path)
    assert K.cmd_up(ns(dry_run=True), trial) == 0
    assert "domain" not in read(trial / "kaggle" / "dataset" / "session.json")
    monkeypatch.setenv("NOVEL_NGROK_DOMAIN", "env-words.ngrok-free.dev")
    assert K.cmd_up(ns(dry_run=True), trial) == 0
    assert read(trial / "kaggle" / "dataset" / "session.json")["domain"] == "env-words.ngrok-free.dev"
    assert not K.domain_file().exists()                                  # nothing is saved by a dry run or by env
    monkeypatch.delenv("NOVEL_NGROK_DOMAIN")
    assert K.cmd_up(ns(dry_run=True, domain="arg-words.ngrok-free.dev"), trial) == 0
    assert read(trial / "kaggle" / "dataset" / "session.json")["domain"] == "arg-words.ngrok-free.dev"
    assert not K.domain_file().exists()                                  # a dry run saves nothing
    assert fake_calls(env) == []


# ---- up

def test_up_with_a_domain_pushes_waits_for_the_server_and_never_reads_the_log(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    with FakeComfy(pending_polls=0) as fc:
        monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", fc.url)           # the "domain" is the local fake
        assert K.cmd_up(ns(domain="Fixed-Words.ngrok-free.dev"), trial) == 0
        out = capsys.readouterr().out
        assert fc.url in out and "the server answers" in out and "saved" in out
        srv = read(trial / "server.json")
        assert srv["url"] == fc.url and srv["domain"] == "fixed-words.ngrok-free.dev"
        assert srv["user"] == "fakeuser" and srv["kernel"] == "fakeuser/coi-ktrial-comfy"
        assert srv["comfy_user"] == "ktrial" and srv["tunnel"] == "ngrok" and srv["pushed"]
        session = read(env.state / "datasets" / "fakeuser__coi-ktrial" / "session.json")
        assert srv["password"] == session["password"] and session["domain"] == "fixed-words.ngrok-free.dev"
        assert fc.paths("GET")[0] == "/system_stats"
        assert "Authorization" in fc.requests[0]["headers"]               # the session's password is sent
        calls = " | ".join(fake_calls(env))
        assert "datasets create" in calls and "kernels push" in calls
        assert not kernel_calls(env, "logs")                              # the log is never read
        assert all("token=...111111" in c for c in fake_calls(env))
        # a running kernel blocks a second up
        monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "100")
        before = len(fake_calls(env))
        assert K.cmd_up(ns(), trial) == 1
        assert "still RUNNING" in capsys.readouterr().out
        assert not any("push" in c or "create" in c or "version" in c for c in fake_calls(env)[before:])
        # after `down` it may start again (a new dataset version); the cloudflared tunnel has no fixed address
        assert K.cmd_down(ns(), trial) == 0
        assert kcomfy.STOP_NAME in fc.uploads
        monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "0")
        assert K.cmd_up(ns(tunnel="cloudflared"), trial) == 0
        assert "datasets version" in " | ".join(fake_calls(env))
        srv = read(trial / "server.json")
        assert srv["tunnel"] == "cloudflared" and "stopped" not in srv and srv["url"] is None and "domain" not in srv
        assert "domain" not in read(env.state / "datasets" / "fakeuser__coi-ktrial" / "session.json")


def test_a_domain_is_remembered_and_used_by_the_next_up(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    with FakeComfy(pending_polls=0) as fc:
        monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", fc.url)
        assert K.cmd_up(ns(domain="https://Remember-Me.ngrok-free.dev/"), trial) == 0
        assert (env.khome / "ngrok-domain").read_text(encoding="utf-8").strip() == "remember-me.ngrok-free.dev"
        capsys.readouterr()
        assert K.cmd_up(ns(), trial) == 0                                 # no option: the file is used
        out = capsys.readouterr().out
        assert "saved" not in out and "domain remember-me.ngrok-free.dev" in out
        assert read(env.state / "datasets" / "fakeuser__coi-ktrial" / "session.json")["domain"] == \
            "remember-me.ngrok-free.dev"
        assert read(trial / "server.json")["domain"] == "remember-me.ngrok-free.dev"
        assert not kernel_calls(env, "logs")


def test_up_with_the_domain_from_the_environment(env, tmp_path, monkeypatch):
    trial = make_trial(tmp_path)
    with FakeComfy(pending_polls=0) as fc:
        monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", fc.url)
        monkeypatch.setenv("NOVEL_NGROK_DOMAIN", "env-words.ngrok-free.dev")
        assert K.cmd_up(ns(), trial) == 0
    assert read(trial / "server.json")["domain"] == "env-words.ngrok-free.dev"
    assert not K.domain_file().exists()


def test_without_an_override_the_address_is_https_and_the_domain(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    monkeypatch.setattr(K, "_comfy", lambda server, **kw: SimpleNamespace(ping=lambda: {}))
    assert K.cmd_up(ns(domain="plain-words.ngrok-free.dev"), trial) == 0
    assert read(trial / "server.json")["url"] == "https://plain-words.ngrok-free.dev"
    assert "https://plain-words.ngrok-free.dev" in capsys.readouterr().out


def test_up_waits_by_polling_the_server_and_reports_progress(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "100")
    with FakeComfy(pending_polls=0, fail_first=8) as fc:                  # four tries of two requests fail
        monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", fc.url)
        assert K.cmd_up(ns(domain="slow-words.ngrok-free.dev"), trial) == 0
    out = capsys.readouterr().out
    assert "still starting" in out and "the server answers" in out
    assert len(kernel_calls(env, "status")) >= 4 and not kernel_calls(env, "logs")


def test_up_no_wait_with_a_domain_does_not_ping(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    with FakeComfy(pending_polls=0) as fc:
        monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", fc.url)
        assert K.cmd_up(ns(domain="nowait-words.ngrok-free.dev", no_wait=True), trial) == 0
        assert fc.requests == []
        assert read(trial / "server.json")["url"] == fc.url and fc.url in capsys.readouterr().out


@pytest.mark.parametrize("kernel_state", ["ERROR", "COMPLETE"])
def test_up_stops_when_the_kernel_ends_before_the_server_answers(env, tmp_path, monkeypatch, capsys, dead,
                                                                 kernel_state):
    trial = make_trial(tmp_path)
    if kernel_state == "ERROR":
        monkeypatch.setenv("FAKE_KAGGLE_ERROR", "Traceback (most recent call last)|AssertionError: gated model")
    url = dead
    monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", url)
    assert K.cmd_up(ns(domain="dead-words.ngrok-free.dev"), trial) == 1
    out = capsys.readouterr().out
    assert kernel_state in out and KERNEL_PAGE in out and "read its log" in out
    assert not kernel_calls(env, "logs")                                  # a running or just-ended log is not trusted
    srv = read(trial / "server.json")
    assert srv["url"] == url and srv["domain"] == "dead-words.ngrok-free.dev"        # stored right after the push


def test_up_gives_up_after_the_wait_limit(env, tmp_path, monkeypatch, capsys, dead):
    trial = make_trial(tmp_path)
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "1000")
    monkeypatch.setenv("NOVEL_KTRIAL_WAIT_MINUTES", "0.0001")
    monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", dead)
    assert K.cmd_up(ns(domain="limit-words.ngrok-free.dev"), trial) == 1
    out = capsys.readouterr().out
    assert "did not answer within" in out and KERNEL_PAGE in out and "RUNNING" in out
    assert not kernel_calls(env, "logs")


def test_up_without_a_domain_pushes_and_tells_what_to_do_without_waiting(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    monkeypatch.setattr(K.time, "sleep", lambda s: pytest.fail("up must not wait without a domain"))
    monkeypatch.setattr(K, "_comfy", lambda *a, **k: pytest.fail("no server is asked without an address"))
    assert K.cmd_up(ns(chapter=24), trial) == 0
    out = capsys.readouterr().out
    assert KERNEL_PAGE in out and "KTRIAL URL https://..." in out and "about 7 minutes" in out
    assert "novel ktrial url --chapter 24 <address>" in out
    assert "novel ktrial up --chapter 24 --domain <your ngrok dev domain>" in out
    assert "https://dashboard.ngrok.com/domains" in out
    srv = read(trial / "server.json")
    assert srv["url"] is None and "domain" not in srv
    assert "domain" not in read(env.state / "datasets" / "fakeuser__coi-ktrial" / "session.json")
    assert not kernel_calls(env, "logs") and not kernel_calls(env, "status")
    assert "kernels push" in " | ".join(fake_calls(env))


def test_up_with_the_cloudflared_tunnel_ignores_a_domain(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    monkeypatch.setenv("NOVEL_NGROK_DOMAIN", "env-words.ngrok-free.dev")
    assert K.cmd_up(ns(tunnel="cloudflared", domain="ignored.ngrok-free.dev"), trial) == 0
    out = capsys.readouterr().out
    assert "only used with the ngrok tunnel" in out and "novel ktrial url --chapter 24" in out
    assert "domain" not in read(trial / "kaggle" / "dataset" / "session.json")
    assert read(trial / "server.json")["url"] is None and not K.domain_file().exists()


# ---- url

def test_url_stores_the_address_and_pings(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    pushed(trial)                                                         # no domain: url is None
    capsys.readouterr()
    with FakeComfy(pending_polls=0) as fc:
        monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", fc.url)
        assert K.cmd_url(SimpleNamespace(chapter=24, address="https://Abcd-1234.ngrok-free.app/"), trial) == 0
        assert fc.paths("GET") == ["/system_stats"]
        out = capsys.readouterr().out
        assert "address stored" in out and "the server answers" in out
        assert read(trial / "server.json")["url"] == fc.url
    assert read(trial / "server.json")["kernel"] == "fakeuser/coi-ktrial-comfy"      # the rest is kept


def test_url_normalises_the_address(env, tmp_path, monkeypatch):
    trial = make_trial(tmp_path)
    write_server(trial, SimpleNamespace(url="x"))
    monkeypatch.setattr(K, "_comfy", lambda server, **kw: SimpleNamespace(ping=lambda: {}))
    for given in ("f2aa-1.ngrok-free.app", "http://F2AA-1.ngrok-free.app", "  https://f2aa-1.ngrok-free.app/  "):
        assert K.cmd_url(SimpleNamespace(chapter=24, address=given), trial) == 0
        assert read(trial / "server.json")["url"] == "https://f2aa-1.ngrok-free.app"
    with pytest.raises(SystemExit, match="not a plain host name"):
        K.cmd_url(SimpleNamespace(chapter=24, address="https://f2aa.app:8443/x"), trial)
    assert read(trial / "server.json")["url"] == "https://f2aa-1.ngrok-free.app"       # unchanged


def test_url_keeps_the_address_when_the_server_does_not_answer(env, tmp_path, monkeypatch, capsys, dead):
    trial = make_trial(tmp_path)
    write_server(trial, SimpleNamespace(url="x"))
    monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", dead)
    assert K.cmd_url(SimpleNamespace(chapter=24, address="a.ngrok-free.app"), trial) == 1
    out = capsys.readouterr().out
    assert "does not answer" in out and "stays stored" in out
    assert read(trial / "server.json")["url"] == dead


def test_url_needs_a_server_json(env, tmp_path, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_url(SimpleNamespace(chapter=24, address="a.ngrok-free.app"), trial) == 1
    assert "novel ktrial up" in capsys.readouterr().out and not (trial / "server.json").exists()


# ---- status

def pushed(trial):
    """A kernel really pushed to the fake Kaggle, no address stored."""
    assert K.cmd_up(ns(), trial) == 0


def set_url(trial, url):
    srv = read(trial / "server.json")
    srv["url"] = url
    (trial / "server.json").write_text(json.dumps(srv), encoding="utf-8")


def test_status_of_a_running_kernel_does_not_read_the_log(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "100")
    monkeypatch.setenv("FAKE_KAGGLE_EMPTY_LOG_WHILE_RUNNING", "1")
    with FakeComfy(pending_polls=0) as fc:
        pushed(trial)
        set_url(trial, fc.url)
        capsys.readouterr()
        assert K.cmd_status(ns(), trial) == 0
        out = capsys.readouterr().out
        assert "RUNNING" in out and f"address: {fc.url}" in out and "the server answers" in out
    assert not kernel_calls(env, "logs") and len(kernel_calls(env, "status")) == 1


def test_status_of_a_running_kernel_without_an_address_says_what_to_do(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "100")
    pushed(trial)
    capsys.readouterr()
    assert K.cmd_status(ns(), trial) == 0
    out = capsys.readouterr().out
    assert KERNEL_PAGE in out and "novel ktrial url --chapter 24 <address>" in out and "--domain" in out
    assert "the server answers" not in out and not kernel_calls(env, "logs")
    assert read(trial / "server.json")["url"] is None


def test_status_of_an_ended_kernel_shows_the_interesting_log_lines(env, tmp_path, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    monkeypatch.setenv("FAKE_KAGGLE_ERROR", "noise|Traceback (most recent call last)|AssertionError: gated model")
    pushed(trial)
    capsys.readouterr()
    assert K.cmd_status(ns(), trial) == 0
    out = capsys.readouterr().out
    assert "ERROR" in out and "AssertionError: gated model" in out and "Traceback" in out and "noise" not in out
    assert "none stored" in out and len(kernel_calls(env, "logs")) == 1
    monkeypatch.delenv("FAKE_KAGGLE_ERROR")                              # COMPLETE: the log is readable too
    assert K.cmd_status(ns(), trial) == 0
    assert "probe 5.0s audio" in capsys.readouterr().out


def test_status_of_an_ended_kernel_whose_server_is_gone(env, tmp_path, capsys, dead):
    trial = make_trial(tmp_path)
    pushed(trial)
    set_url(trial, dead)
    capsys.readouterr()
    assert K.cmd_status(ns(), trial) == 0
    out = capsys.readouterr().out
    assert "COMPLETE" in out and "does not answer" in out and "address: http" in out
    assert K.cmd_status(ns(), tmp_path / "nothing") == 1


def test_down_uploads_the_stop_file_and_marks_server_json(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    assert K.cmd_down(ns(), trial) == 0
    assert kcomfy.STOP_NAME in fc.uploads and fc.uploads[kcomfy.STOP_NAME].startswith(b"\x89PNG")
    srv = read(trial / "server.json")
    assert srv["stopped"] and srv["stop_sent"] is True
    assert "within a minute" in capsys.readouterr().out


def test_down_when_the_server_no_longer_answers(env, tmp_path, capsys):
    trial = make_trial(tmp_path)
    with FakeComfy() as dead:
        url = dead.url
    (trial / "server.json").write_text(json.dumps({"account": "tester", "user": "fakeuser", "kernel": "k/x",
                                                   "comfy_user": "ktrial", "password": "pw", "url": url}))
    assert K.cmd_down(ns(), trial) == 0
    assert "does not answer" in capsys.readouterr().out
    assert read(trial / "server.json")["stopped"]


# ---------------------------------------------------------------- run

def test_run_makes_plates_first_then_shots_with_refs_in_plan_order(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path)
    write_server(trial, fc, comfy_user=None, password=None)
    fc.user = None
    assert K.cmd_run(ns(), trial) == 0
    gs = graphs(fc)
    assert len(gs) == 6
    # plates first, text only, with the plate prompt and seed
    assert [seed_of(g) for g in gs[:2]] == [11, 22] and [text_of(g) for g in gs[:2]] == ["Place g01, empty.",
                                                                                          "Place g02, empty."]
    assert all(not load_refs(g) for g in gs[:2])
    # then the shots, references in plan order, under the names the plan rules give
    assert [seed_of(g) for g in gs[2:]] == [101, 102, 103, 104]
    assert load_refs(gs[2]) == ["ktrial-lumian_lee.jpg", "ktrial-c0024-g01.jpg"]
    assert load_refs(gs[3]) == ["ktrial-lumian_lee.jpg", "ktrial-aurore_lee.jpg", "ktrial-c0024-g01.jpg"]
    assert load_refs(gs[4]) == ["ktrial-c0024-g02.jpg"]
    assert load_refs(gs[5]) == ["ktrial-aurore_lee.jpg", "ktrial-c0024-g02.jpg"]
    assert text_of(gs[2]) == "One person <ref_image_0> in <ref_image_1>"
    assert (node(gs[2], "EmptyFlux2LatentImage")["width"], node(gs[2], "EmptyFlux2LatentImage")["height"]) == (W, H)
    # each reference file is uploaded once per run
    uploads = [r for r in fc.requests if r["path"] == "/upload/image"]
    assert len(uploads) == 4 and sorted(fc.uploads) == ["ktrial-aurore_lee.jpg", "ktrial-c0024-g01.jpg",
                                                         "ktrial-c0024-g02.jpg", "ktrial-lumian_lee.jpg"]
    # outputs: real JPEGs, meta files, one log line each
    for rel in ("plates/g01", "plates/g02", "images/s01", "images/s02", "images/s03", "images/s04"):
        assert K._valid_jpeg(trial / f"{rel}.jpg")
        with Image.open(trial / f"{rel}.jpg") as im:
            assert im.format == "JPEG" and im.size == (W, H)
        meta = read(trial / f"{rel}.meta.json")
        assert {"id", "seed", "seconds", "refs", "width", "height", "bytes", "model", "steps", "at"} <= set(meta)
        assert meta["model"] == kcomfy.UNET_NAME and meta["steps"] == kcomfy.STEPS
    m2 = read(trial / "images" / "s02.meta.json")
    assert m2["id"] == "s02" and m2["seed"] == 102 and m2["refs"] == ["lumian_lee.jpg", "aurore_lee.jpg", "g01.jpg"]
    log = [json.loads(l) for l in (trial / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["id"] for e in log] == ["g01", "g02", "s01", "s02", "s03", "s04"]
    out = capsys.readouterr().out
    assert "made 6, skipped 0, failed 0" in out and "400 pictures would take" in out and "left" in out


def test_second_run_skips_everything(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(), trial) == 0
    n = len(fc.prompts)
    uploads = len(fc.uploads)
    capsys.readouterr()
    assert K.cmd_run(ns(), trial) == 0
    assert len(fc.prompts) == n and len(fc.uploads) == uploads
    assert "made 0, skipped 6, failed 0" in capsys.readouterr().out
    assert len((trial / "log.jsonl").read_text(encoding="utf-8").splitlines()) == 6


def test_only_a_shot_makes_its_plate_and_that_shot(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(only="s02"), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [11, 102]
    assert not (trial / "images" / "s01.jpg").exists() and not (trial / "plates" / "g02.jpg").exists()


def test_only_a_group_selects_all_its_shots(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(only="g02"), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [22, 103, 104]


def test_only_mixes_shots_and_groups_and_rejects_unknown(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(only="s01, g02"), trial) == 0
    assert sorted(seed_of(g) for g in graphs(fc)) == [11, 22, 101, 103, 104]
    with pytest.raises(SystemExit, match="s99"):
        K.cmd_run(ns(only="s99"), trial)


def test_limit_keeps_the_first_selected_shots(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(limit=3), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [11, 22, 101, 102, 103]       # g01, g02, then s01..s03
    assert not (trial / "images" / "s04.jpg").exists()
    fc.prompts.clear()
    assert K.cmd_run(ns(only="g02", limit=1), trial) == 0                    # s03 exists: nothing to make
    assert len(fc.prompts) == 0


def test_plates_only(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(plates_only=True), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [11, 22]
    assert not list((trial / "images").glob("*.jpg"))
    assert (trial / "plates" / "g01.jpg").exists()


def test_reroll_uses_next_seed_and_moves_the_old_picture(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(), trial) == 0
    old_bytes = (trial / "images" / "s02.jpg").read_bytes()
    fc.prompts.clear()
    assert K.cmd_run(ns(only="s02", reroll=True), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [103]                         # no plate, seed 102 + 1
    assert read(trial / "images" / "s02.meta.json")["seed"] == 103
    moved = list((trial / "images" / "old").rglob("s02.jpg"))
    assert len(moved) == 1 and moved[0].read_bytes() == old_bytes
    assert list((trial / "images" / "old").rglob("s02.meta.json"))
    assert (trial / "images" / "s01.jpg").exists() and K._valid_jpeg(trial / "images" / "s02.jpg")
    fc.prompts.clear()
    assert K.cmd_run(ns(only="s02", reroll=True), trial) == 0                # reads the seed from the meta: 103 + 1
    assert [seed_of(g) for g in graphs(fc)] == [104]
    assert len(list((trial / "images" / "old").rglob("s02.jpg"))) == 2       # nothing was overwritten


def test_redo_keeps_the_seed(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(), trial) == 0
    fc.prompts.clear()
    assert K.cmd_run(ns(only="s03", redo=True), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [103]
    assert read(trial / "images" / "s03.meta.json")["seed"] == 103
    assert len(list((trial / "images" / "old").rglob("s03.jpg"))) == 1


def test_reroll_with_a_group_also_remakes_its_plate(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(), trial) == 0
    fc.prompts.clear()
    assert K.cmd_run(ns(only="g02", reroll=True), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [23, 104, 105]                # plate 22 + 1, shots 103 + 1, 104 + 1
    assert len(list((trial / "plates" / "old").rglob("g02.jpg"))) == 1
    assert read(trial / "plates" / "g02.meta.json")["seed"] == 23
    assert (trial / "plates" / "g01.jpg").exists() and not (trial / "plates" / "old" / "g01.jpg").exists()
    # the new plate is what the shots got as place reference
    assert load_refs(graphs(fc)[1]) == ["ktrial-c0024-g02.jpg"]
    assert fc.uploads["ktrial-c0024-g02.jpg"] == (trial / "plates" / "g02.jpg").read_bytes()


def test_a_failing_shot_is_logged_and_the_run_goes_on(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path, prompt_s02="BOOM")
    write_server(trial, fc)
    fc.user = None
    fc.exec_error_if = lambda g: g["4"]["inputs"]["text"] == "BOOM"
    assert K.cmd_run(ns(), trial) == 1
    out = capsys.readouterr().out
    assert "s02  FAILED" in out and "CUDA out of memory" in out and "failed 1" in out
    assert not (trial / "images" / "s02.jpg").exists() and not (trial / "images" / "s02.jpg.part").exists()
    assert all((trial / "images" / f"s0{n}.jpg").exists() for n in (1, 3, 4))
    log = [json.loads(l) for l in (trial / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    err = [e for e in log if e.get("error")]
    assert len(err) == 1 and err[0]["id"] == "s02" and "CUDA" in err[0]["error"]
    # the next run only retries what is missing
    fc.exec_error_if = None
    fc.prompts.clear()
    assert K.cmd_run(ns(), trial) == 0
    assert [seed_of(g) for g in graphs(fc)] == [102]


def test_three_failures_in_a_row_stop_the_run(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    fc.exec_error_if = lambda g: any(n["class_type"] == "ReferenceMultiLatent" for n in g.values())
    assert K.cmd_run(ns(), trial) == 1
    assert "3 failures in a row" in capsys.readouterr().out
    assert len(fc.prompts) == 5                                              # 2 plates + s01, s02, s03; s04 never tried
    ids = [json.loads(l)["id"] for l in (trial / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ids == ["g01", "g02", "s01", "s02", "s03"]


def test_ctrl_c_stops_cleanly_with_130(env, tmp_path, fc, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    real, calls = K._Run.make, []

    def make(self, *a, **kw):
        calls.append(a[1])
        if len(calls) == 4:
            raise KeyboardInterrupt
        return real(self, *a, **kw)

    monkeypatch.setattr(K._Run, "make", make)
    assert K.cmd_run(ns(), trial) == 130
    assert "Ctrl+C" in capsys.readouterr().out
    assert K._valid_jpeg(trial / "images" / "s01.jpg") and not (trial / "images" / "s02.jpg").exists()


def test_a_tiny_picture_is_kept_but_flagged(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path, width=64, height=48)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(only="s03"), trial) == 0
    assert "WARNING small file" in capsys.readouterr().out
    assert K._valid_jpeg(trial / "images" / "s03.jpg")
    fc.prompts.clear()
    assert K.cmd_run(ns(only="s03"), trial) == 0                             # small is not "missing"
    assert len(fc.prompts) == 0


def test_run_needs_a_plan_a_server_and_a_live_server(env, tmp_path, fc, capsys):
    assert K.cmd_run(ns(), tmp_path / "nothing") == 1
    assert "plan.json" in capsys.readouterr().out
    trial = make_trial(tmp_path)
    assert K.cmd_run(ns(), trial) == 1
    assert "novel ktrial up" in capsys.readouterr().out
    write_server(trial, fc)
    fc.user = None
    fc.stop()
    assert K.cmd_run(ns(), trial) == 1
    assert "does not answer" in capsys.readouterr().out
    assert not (trial / "log.jsonl").exists()


# ---------------------------------------------------------------- report

def aspect(path: Path) -> float:
    with Image.open(path) as im:
        return im.width / im.height


def test_crop_box_is_the_largest_window_centred_on_focus_and_clamped():
    assert K.crop_box(1376, 768, 1.0, None) == (304, 0, 1072, 768)
    assert K.crop_box(1376, 768, 1.0, [0, 0]) == (0, 0, 768, 768)
    assert K.crop_box(1376, 768, 1.0, [1, 1]) == (608, 0, 1376, 768)
    left, top, right, bottom = K.crop_box(1376, 768, 16 / 9, [0.3, 0.9])
    assert left == 0 and right <= 1376 and abs((right - left) / (bottom - top) - 16 / 9) < 0.02
    left, top, right, bottom = K.crop_box(1000, 1000, 2 / 3, [0.5, 0.9])         # a narrow window in a square
    assert bottom == 1000 and top == 0 and abs((right - left) / (bottom - top) - 2 / 3) < 0.01
    left, top, right, bottom = K.crop_box(1376, 768, 4 / 3, [0.2, 0.5])
    assert abs((right - left) / (bottom - top) - 4 / 3) < 0.01 and left == 0     # focus too far left: clamped


def test_report_with_no_pictures(env, tmp_path, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_report(ns(), trial) == 0
    page = (trial / "report.html").read_text(encoding="utf-8")
    for n in (1, 2, 3, 4):
        assert f"Window {n}" in page
    assert "विंडो एक" in page and "0/4" in page
    assert page.count("not generated yet") >= 2 * 4 + 2 and page.count("no Flow picture") >= 2 * 2
    assert "Place g01" in page and "moon" in page
    assert "<script" not in page.replace("&lt;script&gt;", "") and "base64" not in page
    assert (trial / "report" / "cut" / "flow-s01.jpg").exists() and (trial / "report" / "cut" / "flow-s03.jpg").exists()
    assert not list((trial / "report" / "cut").glob("trial-*"))
    assert "file:///" in page
    assert not (trial / "server.json").exists()


def test_report_with_some_pictures(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc)
    fc.user = None
    assert K.cmd_run(ns(only="s01,s02"), trial) == 0
    assert K.cmd_report(ns(), trial) == 0
    page = (trial / "report.html").read_text(encoding="utf-8")
    for n in (1, 2, 3, 4):
        assert f"Window {n}" in page
    cut = trial / "report" / "cut"
    assert (cut / "trial-s01.jpg").exists() and (cut / "trial-s02.jpg").exists()
    assert not (cut / "trial-s03.jpg").exists() and not (cut / "flow-s02.jpg").exists()
    # trial shapes come from the trial scenes: one person -> portrait 3:4, two people medium -> landscape 4:3
    assert abs(aspect(cut / "trial-s01.jpg") / (3 / 4) - 1) < 0.02
    assert abs(aspect(cut / "trial-s02.jpg") / (4 / 3) - 1) < 0.02
    # Flow shapes come from the PRODUCTION scenes: window 1 has two people -> 4:3, window 3 close-up -> tall 2:3
    assert abs(aspect(cut / "flow-s01.jpg") / (4 / 3) - 1) < 0.02
    assert abs(aspect(cut / "flow-s03.jpg") / (2 / 3) - 1) < 0.02
    for f in cut.glob("*.jpg"):
        with Image.open(f) as im:
            assert abs(im.height - K.CUT_HEIGHT) <= 1
    # pictures by relative path, missing ones as boxes, the prompt escaped, numbers in the header
    assert 'src="images/s01.jpg"' in page and 'src="report/cut/trial-s01.jpg"' in page
    assert 'src="plates/g01.jpg"' in page and 'src="plates/g02.jpg"' not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page and "<script>x" not in page and "&amp;" in page
    assert "2/4" in page and "seed 102" in page and "Lumian Lee, Aurore Lee" in page
    assert "not generated yet" in page and "no Flow picture" in page
    assert "<details>" in page and "<summary>prompt" in page


def test_report_survives_a_missing_production_scene_file(env, tmp_path):
    trial = make_trial(tmp_path)
    plan = read(trial / "plan.json")
    plan["production_scenes"] = str(tmp_path / "gone.json")
    (trial / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    assert K.cmd_report(ns(), trial) == 0
    assert (trial / "report" / "cut" / "flow-s01.jpg").exists()               # falls back to the trial scene's shape
    assert K.cmd_report(ns(), tmp_path / "nothing") == 1


# ---------------------------------------------------------------- run --via openrouter

@pytest.fixture
def orv(tmp_path, monkeypatch):
    """A fake OpenRouter, the key in the environment, no sleeping between retries."""
    monkeypatch.setattr(korouter, "_sleep", lambda s: None)
    with FakeOpenRouter(key="sk-or-test-1234567", cost=0.009) as f:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234567")
        monkeypatch.setenv("NOVEL_OPENROUTER_URL", f.url)
        yield f


def ons(**kw):
    return ns(**{"via": "openrouter", "resolution": "1K", "budget": 1.5, **kw})


def ref_image(entry) -> Image.Image:
    url = entry["image_url"]["url"]
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")


def same_picture(img: Image.Image, path: Path) -> bool:
    with Image.open(path) as other:
        other = other.convert("RGB")
        if other.size != img.size:
            return False
        diff = ImageChops.difference(img, other).convert("L")
        return ImageStat.Stat(diff).mean[0] < 6


def bodies(fo):
    return [r["body"] for r in fo.requests]


def test_or_run_makes_plates_then_shots_into_the_openrouter_folder(env, tmp_path, orv, capsys):
    trial = make_trial(tmp_path)
    kaggle_plate, kaggle_shot = trial / "plates" / "g01.jpg", trial / "images" / "s01.jpg"
    jpg(kaggle_plate, 64, 64, seed=50)
    jpg(kaggle_shot, 64, 64, seed=51)
    before = kaggle_plate.read_bytes(), kaggle_shot.read_bytes()
    assert not (trial / "server.json").exists()
    assert K.cmd_run(ons(), trial) == 0
    bs = bodies(orv)
    assert len(bs) == 6
    # plates first, text only, plate prompt and plan seed
    assert [b["prompt"] for b in bs[:2]] == ["Place g01, empty.", "Place g02, empty."]
    assert all("input_references" not in b for b in bs[:2])
    assert [b["seed"] for b in bs] == [11, 22, 101, 102, 103, 104]
    # shots: hosted wording, references in plan order, a place reference is the OPENROUTER plate
    assert bs[2]["prompt"] == "One person image 1 in image 2"
    assert bs[3]["prompt"] == "Two people image 1 & image 2 <script>x</script>"
    assert [len(b.get("input_references", [])) for b in bs[2:]] == [2, 3, 1, 2]
    assert all(b["resolution"] == "1K" and b["aspect_ratio"] == "16:9" and
               b["model"] == korouter.MODEL for b in bs)
    plate1 = trial / K.OR_DIR / "plates" / "g01.jpg"
    assert same_picture(ref_image(bs[2]["input_references"][0]), trial / "refs" / "lumian_lee.jpg")
    assert same_picture(ref_image(bs[2]["input_references"][1]), plate1)
    assert not same_picture(ref_image(bs[2]["input_references"][1]), kaggle_plate)
    refs3 = bs[3]["input_references"]
    assert same_picture(ref_image(refs3[0]), trial / "refs" / "lumian_lee.jpg")
    assert same_picture(ref_image(refs3[1]), trial / "refs" / "aurore_lee.jpg")
    assert same_picture(ref_image(refs3[2]), plate1)
    assert same_picture(ref_image(bs[4]["input_references"][0]), trial / K.OR_DIR / "plates" / "g02.jpg")
    assert all(r["headers"]["Authorization"] == "Bearer sk-or-test-1234567" for r in orv.requests)
    # outputs: JPEG files and meta files under openrouter/, the Kaggle pictures untouched
    for rel in ("plates/g01", "plates/g02", "images/s01", "images/s02", "images/s03", "images/s04"):
        out = trial / K.OR_DIR / f"{rel}.jpg"
        assert K._valid_jpeg(out)
        with Image.open(out) as im:
            assert im.format == "JPEG" and im.size == (W, H)
        meta = read(trial / K.OR_DIR / f"{rel}.meta.json")
        assert {"id", "seed", "seconds", "refs", "width", "height", "bytes", "model", "resolution", "cost", "via",
                "at"} <= set(meta)
        assert meta["via"] == "openrouter" and meta["cost"] == 0.009 and meta["resolution"] == "1K"
        assert meta["model"] == korouter.MODEL
    m2 = read(trial / K.OR_DIR / "images" / "s02.meta.json")
    assert m2["id"] == "s02" and m2["seed"] == 102 and m2["refs"] == ["lumian_lee.jpg", "aurore_lee.jpg", "g01.jpg"]
    assert (kaggle_plate.read_bytes(), kaggle_shot.read_bytes()) == before
    assert sorted(p.name for p in (trial / "images").glob("*.jpg")) == ["s01.jpg"]
    assert not (trial / "log.jsonl").exists()
    log = [json.loads(l) for l in (trial / K.OR_DIR / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["id"] for e in log] == ["g01", "g02", "s01", "s02", "s03", "s04"]
    assert all(e["cost"] == 0.009 and e["via"] == "openrouter" for e in log)
    out = capsys.readouterr().out
    assert "made 6, skipped 0, failed 0" in out and "cost of this run: $0.0540" in out
    assert "total $0.0540 of $1.50" in out and "400 pictures would take" not in out


def test_or_resolution_option_is_sent(env, tmp_path, orv):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(resolution="2K", only="s03"), trial) == 0
    assert {b["resolution"] for b in bodies(orv)} == {"2K"}
    assert read(trial / K.OR_DIR / "images" / "s03.meta.json")["resolution"] == "2K"


def test_or_cost_falls_back_to_the_estimate_when_the_answer_has_none(env, tmp_path, orv, capsys):
    orv.cost = None
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(only="s03"), trial) == 0
    assert read(trial / K.OR_DIR / "images" / "s03.meta.json")["cost"] == korouter.COST_PER_IMAGE
    assert "cost of this run: $0.0280" in capsys.readouterr().out


def test_or_second_run_skips_existing_pictures(env, tmp_path, orv, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(), trial) == 0
    capsys.readouterr()
    assert K.cmd_run(ons(), trial) == 0
    assert len(orv.requests) == 6
    assert "made 0, skipped 6, failed 0" in capsys.readouterr().out
    assert len((trial / K.OR_DIR / "log.jsonl").read_text(encoding="utf-8").splitlines()) == 6


def test_or_only_and_limit(env, tmp_path, orv):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(only="s02"), trial) == 0
    assert [b["seed"] for b in bodies(orv)] == [11, 102]
    assert not (trial / K.OR_DIR / "images" / "s01.jpg").exists()
    orv.requests.clear()
    assert K.cmd_run(ons(only="g02"), trial) == 0
    assert [b["seed"] for b in bodies(orv)] == [22, 103, 104]
    orv.requests.clear()
    assert K.cmd_run(ons(limit=1), trial) == 0                       # s01 is the only one selected
    assert [b["seed"] for b in bodies(orv)] == [101]
    with pytest.raises(SystemExit, match="s99"):
        K.cmd_run(ons(only="s99"), trial)


def test_or_plates_only(env, tmp_path, orv):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(plates_only=True), trial) == 0
    assert [b["seed"] for b in bodies(orv)] == [11, 22]
    assert not list((trial / K.OR_DIR / "images").glob("*.jpg"))


def test_or_reroll_moves_the_old_picture_and_uses_the_next_seed(env, tmp_path, orv):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(only="s02"), trial) == 0
    old_bytes = (trial / K.OR_DIR / "images" / "s02.jpg").read_bytes()
    orv.requests.clear()
    assert K.cmd_run(ons(only="s02", reroll=True), trial) == 0
    assert [b["seed"] for b in bodies(orv)] == [103]                 # the plate is not named, so it is kept
    assert read(trial / K.OR_DIR / "images" / "s02.meta.json")["seed"] == 103
    olds = list((trial / K.OR_DIR / "images" / "old").glob("*/s02.jpg"))
    assert len(olds) == 1 and olds[0].read_bytes() == old_bytes
    assert read(olds[0].with_name("s02.meta.json"))["seed"] == 102
    assert (trial / K.OR_DIR / "images" / "s02.jpg").read_bytes() != old_bytes
    assert not (trial / "images" / "old").exists()


def test_or_redo_keeps_the_seed_and_a_group_reroll_remakes_its_plate(env, tmp_path, orv):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(only="s01"), trial) == 0
    orv.requests.clear()
    assert K.cmd_run(ons(only="s01", redo=True), trial) == 0
    assert [b["seed"] for b in bodies(orv)] == [101]
    assert len(list((trial / K.OR_DIR / "images" / "old").glob("*/s01.jpg"))) == 1
    orv.requests.clear()
    assert K.cmd_run(ons(only="g01", reroll=True), trial) == 0
    # plate g01 +1; s01 (made with 101) +1; s02 was never made here, so its plan seed 102 +1
    assert [b["seed"] for b in bodies(orv)] == [12, 102, 103]
    assert len(list((trial / K.OR_DIR / "plates" / "old").glob("*/g01.jpg"))) == 1


def test_or_budget_stops_cleanly_before_the_picture_that_would_pass_it(env, tmp_path, orv, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(budget=0.03), trial) == 0                   # 3 x 0.009 = 0.027; a 4th would make 0.036
    out = capsys.readouterr().out
    assert len(orv.requests) == 3 and "would pass --budget $0.03" in out
    assert "made 3" in out and "cost of this run: $0.0270" in out
    assert K._valid_jpeg(trial / K.OR_DIR / "images" / "s01.jpg") and \
        not (trial / K.OR_DIR / "images" / "s02.jpg").exists()
    # the budget is per run: a second run starts from zero and carries on
    orv.requests.clear()
    assert K.cmd_run(ons(budget=0.03), trial) == 0
    assert [b["seed"] for b in bodies(orv)] == [102, 103, 104]
    assert K._valid_jpeg(trial / K.OR_DIR / "images" / "s04.jpg")


def test_or_budget_counts_the_real_cost_not_the_estimate(env, tmp_path, orv, capsys):
    orv.cost = 0.02
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(budget=0.05), trial) == 0                   # 2 pictures = 0.04, 0.04 + 0.009 fits, 0.06 stops
    assert len(orv.requests) == 3
    assert "cost of this run: $0.0600" in capsys.readouterr().out


def test_or_a_budget_too_small_for_one_picture_makes_nothing(env, tmp_path, orv, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(budget=0.005), trial) == 0
    assert orv.requests == [] and "cost of this run: $0.0000" in capsys.readouterr().out


def test_or_a_failing_shot_is_logged_and_the_run_goes_on(env, tmp_path, orv, capsys):
    orv.fail_if = lambda b: 400 if b["seed"] == 102 else None
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(), trial) == 1
    out = capsys.readouterr().out
    assert "s02  FAILED" in out and "fake failure 400" in out and "made 5, skipped 0, failed 1" in out
    assert K._valid_jpeg(trial / K.OR_DIR / "images" / "s03.jpg")
    assert not (trial / K.OR_DIR / "images" / "s02.jpg").exists()
    errs = [json.loads(l) for l in (trial / K.OR_DIR / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["id"] for e in errs if e.get("error")] == ["s02"] and "cost of this run: $0.0450" in out


def test_or_three_failures_in_a_row_stop_the_run(env, tmp_path, orv, capsys):
    orv.fail_if = lambda b: 400 if "input_references" in b else None
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(), trial) == 1
    assert "3 failures in a row" in capsys.readouterr().out
    assert len(orv.requests) == 5                                    # 2 plates, s01, s02, s03; s04 never tried
    ids = [json.loads(l)["id"] for l in (trial / K.OR_DIR / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ids == ["g01", "g02", "s01", "s02", "s03"]


def test_or_a_failed_plate_skips_its_shots_only(env, tmp_path, orv, capsys):
    orv.fail_if = lambda b: 400 if b["seed"] == 11 else None
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(), trial) == 1
    out = capsys.readouterr().out
    assert "s01: skipped, its place picture g01 is not available" in out
    assert K._valid_jpeg(trial / K.OR_DIR / "images" / "s03.jpg")
    assert not (trial / K.OR_DIR / "images" / "s01.jpg").exists()


def test_or_retries_a_403_inside_the_client(env, tmp_path, orv):
    orv.script = [(403, {"error": {"message": "forbidden once"}})]
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(only="s03"), trial) == 0
    assert len(orv.requests) == 3 and K._valid_jpeg(trial / K.OR_DIR / "images" / "s03.jpg")


def test_or_ctrl_c_stops_cleanly_with_130(env, tmp_path, orv, monkeypatch, capsys):
    trial = make_trial(tmp_path)
    real, calls = K._RouterRun.make, []

    def make(self, *a, **kw):
        calls.append(a[1])
        if len(calls) == 4:
            raise KeyboardInterrupt
        return real(self, *a, **kw)

    monkeypatch.setattr(K._RouterRun, "make", make)
    assert K.cmd_run(ons(), trial) == 130
    assert "Ctrl+C" in capsys.readouterr().out
    assert K._valid_jpeg(trial / K.OR_DIR / "images" / "s01.jpg")
    assert not (trial / K.OR_DIR / "images" / "s02.jpg").exists()


def test_or_url_answers_and_a_missing_reference_work_too(env, tmp_path, orv, capsys):
    orv.mode = "url"
    trial = make_trial(tmp_path)
    (trial / "refs" / "aurore_lee.jpg").unlink()
    assert K.cmd_run(ons(only="s02,s03"), trial) == 1                # s02 needs aurore_lee, s03 does not
    out = capsys.readouterr().out
    assert "reference refs/aurore_lee.jpg is missing" in out
    assert K._valid_jpeg(trial / K.OR_DIR / "images" / "s03.jpg")
    assert not (trial / K.OR_DIR / "images" / "s02.jpg").exists()
    assert len(orv.downloads) == len(orv.requests) == 3              # g01, g02, s03; s02 never left the machine


def test_or_without_a_key_stops_before_anything_is_made(env, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(korouter, "EARLIER_TEST_ENV", tmp_path / "no-such.env")
    trial = make_trial(tmp_path)
    assert K.cmd_run(ons(), trial) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().out
    assert not (trial / K.OR_DIR).exists()


def test_run_via_kaggle_stays_the_default_and_needs_its_server(env, tmp_path, orv, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_run(ns(via="kaggle"), trial) == 1
    assert "novel ktrial up" in capsys.readouterr().out
    assert K.cmd_run(ns(), trial) == 1                               # no `via` at all: kaggle
    assert orv.requests == [] and not (trial / K.OR_DIR).exists()


def test_or_run_with_a_server_json_present_does_not_touch_the_server(env, tmp_path, orv):
    with FakeComfy(pending_polls=0) as fc:
        trial = make_trial(tmp_path)
        write_server(trial, fc)
        assert K.cmd_run(ons(only="s03"), trial) == 0
        assert fc.requests == [] and fc.prompts == {}


# ---------------------------------------------------------------- report with the OpenRouter column

def test_report_has_the_openrouter_column_only_when_its_folder_exists(env, tmp_path, orv):
    trial = make_trial(tmp_path)
    assert K.cmd_report(ns(), trial) == 0
    page = (trial / "report.html").read_text(encoding="utf-8")
    assert "OpenRouter" not in page and "cols3" not in page.split("</style>")[1]
    assert "<h4>FLUX.3 klein (Kaggle T4)</h4>" in page and "<h4>Flow</h4>" in page
    assert K.cmd_run(ons(only="s01,s02"), trial) == 0
    assert K.cmd_report(ns(), trial) == 0
    page = (trial / "report.html").read_text(encoding="utf-8")
    assert "<h4>FLUX.3 klein (OpenRouter)</h4>" in page and "<h4>FLUX.3 klein (Kaggle T4)</h4>" in page
    assert page.count("<h4>FLUX.3 klein (OpenRouter)</h4>") == 4                  # one per window
    assert 'class="cols cols3"' in page
    # the header line: count, average seconds, total cost (1 plate + 2 shots here)
    assert f"OpenRouter {korouter.MODEL}" in page and "2/4" in page and "$0.0270" in page
    cut = trial / "report" / "cut"
    assert (cut / "or-s01.jpg").exists() and (cut / "or-s02.jpg").exists() and not (cut / "or-s03.jpg").exists()
    # the same panel shapes as the Kaggle column: one person -> portrait 3:4, two people medium -> landscape 4:3
    assert abs(aspect(cut / "or-s01.jpg") / (3 / 4) - 1) < 0.02
    assert abs(aspect(cut / "or-s02.jpg") / (4 / 3) - 1) < 0.02
    assert f'src="{K.OR_DIR}/images/s01.jpg"' in page and 'src="report/cut/or-s01.jpg"' in page
    assert f'src="{K.OR_DIR}/images/s03.jpg"' not in page
    # a place picture of each kind side by side: only g01 has an OpenRouter plate, no Kaggle plate exists yet
    assert f'src="{K.OR_DIR}/plates/g01.jpg"' in page and f'src="{K.OR_DIR}/plates/g02.jpg"' not in page
    assert 'src="plates/g01.jpg"' not in page
    assert "Place g01, empty." not in page and "One person image 1 in image 2" in page       # prompt as sent
    assert "$0.0090" in page and "seed 102" in page


def test_report_shows_both_plates_side_by_side_and_survives_missing_or_pictures(env, tmp_path, orv):
    trial = make_trial(tmp_path)
    (trial / K.OR_DIR / "images").mkdir(parents=True)            # the folder alone makes the column appear
    jpg(trial / "plates" / "g01.jpg", 64, 64, seed=9)
    assert K.cmd_report(ns(), trial) == 0
    page = (trial / "report.html").read_text(encoding="utf-8")
    assert page.count("<h4>FLUX.3 klein (OpenRouter)</h4>") == 4
    assert 'src="plates/g01.jpg"' in page and f"{K.OR_DIR}/plates" not in page
    assert f"OpenRouter {korouter.MODEL}" in page and "0/4" in page and "$0.0000" in page
    assert not list((trial / "report" / "cut").glob("or-*"))


# ---------------------------------------------------------------- MiniMax-H3-Image (second model, own folder)

MM = kcomfy.MINIMAX


def test_up_model_minimax_is_stored_in_session_and_server_json(env, tmp_path, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_up(ns(dry_run=True, model=MM), trial) == 0
    assert "model minimax-h3" in capsys.readouterr().out
    assert read(trial / "kaggle" / "dataset" / "session.json")["model"] == MM
    assert (trial / "kaggle" / "dataset" / "kcomfy.py").read_bytes() == (REPO / "novel" / "kcomfy.py").read_bytes()
    # FLUX stays the default: no key at all
    assert K.cmd_up(ns(dry_run=True, model="flux"), trial) == 0
    assert "model" not in read(trial / "kaggle" / "dataset" / "session.json")
    assert K.cmd_up(ns(dry_run=True), trial) == 0
    assert "model" not in read(trial / "kaggle" / "dataset" / "session.json")


def test_up_pushes_and_records_the_model(env, tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_KTRIAL_URL_OVERRIDE", "http://127.0.0.1:9")
    trial = make_trial(tmp_path)
    assert K.cmd_up(ns(model=MM, no_wait=True, domain="x.ngrok-free.dev"), trial) == 0
    assert read(trial / "server.json")["model"] == MM


def test_minimax_run_writes_its_own_folder_and_leaves_the_flux_pictures(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path)
    write_server(trial, fc, comfy_user=None, password=None, model=MM)
    fc.user = None
    jpg(trial / "images" / "s01.jpg", seed=9)                       # an earlier FLUX picture
    flux_bytes = (trial / "images" / "s01.jpg").read_bytes()
    assert K.cmd_run(ns(), trial) == 0
    root = trial / kcomfy.MM_DIR
    assert (trial / "images" / "s01.jpg").read_bytes() == flux_bytes          # not touched, not skipped over
    assert not (trial / "plates" / "g01.jpg").exists()
    gs = graphs(fc)
    assert len(gs) == 6                                                      # s01 is made too: its own folder is empty
    for g in gs:
        assert {n["class_type"] for n in g["graph"].values()} >= {"MiniMaxH3ImageConditioning", "KTrialUnetLoaderGGUF"}
    cond = lambda g: node(g, "MiniMaxH3ImageConditioning")
    assert (cond(gs[0])["width"], cond(gs[0])["height"]) == (W, H) and cond(gs[0])["prompt"] == "Place g01, empty."
    # references: the plate comes from the MiniMax folder, wording is <Picture n>
    assert cond(gs[2])["prompt"] == "One person <Picture 1> in <Picture 2>"
    assert load_refs(gs[2]) == ["ktrial-lumian_lee.jpg", "ktrial-c0024-g01.jpg"]
    assert fc.uploads["ktrial-c0024-g01.jpg"] == (root / "plates" / "g01.jpg").read_bytes()
    for rel in ("plates/g01", "plates/g02", "images/s01", "images/s02", "images/s03", "images/s04"):
        assert K._valid_jpeg(root / f"{rel}.jpg")
        meta = read(root / f"{rel}.meta.json")
        assert meta["model"] == kcomfy.MM_UNET_NAME and meta["text_encoder"] == kcomfy.MM_CLIP_NAME
        assert meta["steps"] == 6 and meta["seconds"] is not None
        assert (root / f"{rel}.prompt.txt").read_text(encoding="utf-8")        # the prompt that was sent
    assert "<Picture 1>" in (root / "images" / "s01.prompt.txt").read_text(encoding="utf-8")
    assert "<ref_image" in (trial / "images" / "s01.prompt.txt").read_text(encoding="utf-8")   # the file on disk stays FLUX's
    log = [json.loads(l) for l in (root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["id"] for e in log] == ["g01", "g02", "s01", "s02", "s03", "s04"] and not (trial / "log.jsonl").exists()
    assert f"pictures go to {root}" in capsys.readouterr().out


def test_minimax_reroll_archives_picture_meta_and_prompt(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc, comfy_user=None, password=None, model=MM)
    fc.user = None
    assert K.cmd_run(ns(only="s03"), trial) == 0
    root = trial / kcomfy.MM_DIR
    assert K.cmd_run(ns(only="s03", reroll=True), trial) == 0
    olds = list((root / "images" / "old").rglob("s03.*"))
    assert sorted(p.name for p in olds) == ["s03.jpg", "s03.meta.json", "s03.prompt.txt"]
    assert read(root / "images" / "s03.meta.json")["seed"] == 104        # next seed of the plan's 103


# ---------------------------------------------------------------- Qwen-Image-2.1-Turbo (third model, own folder)

QW = kcomfy.QWEN


def test_up_model_qwen_and_a_second_ngrok_dataset_are_staged(env, tmp_path, capsys):
    trial = make_trial(tmp_path)
    assert K.cmd_up(ns(dry_run=True, model=QW, ngrok_dataset="ngrok-token-2"), trial) == 0
    assert "model qwen-image-turbo" in capsys.readouterr().out
    session = read(trial / "kaggle" / "dataset" / "session.json")
    assert session["model"] == QW and session["ngrok_dataset"] == "ngrok-token-2"
    meta = read(trial / "kaggle" / "kernel" / "kernel-metadata.json")
    assert meta["dataset_sources"] == ["fakeuser/coi-ktrial", "fakeuser/hf-token", "fakeuser/ngrok-token-2"]
    # default: the stored dataset name, nothing in the session
    assert K.cmd_up(ns(dry_run=True), trial) == 0
    assert "ngrok_dataset" not in read(trial / "kaggle" / "dataset" / "session.json")
    assert read(trial / "kaggle" / "kernel" / "kernel-metadata.json")["dataset_sources"][-1] == "fakeuser/ngrok-token"
    with pytest.raises(SystemExit, match="slug"):
        K.cmd_up(ns(dry_run=True, ngrok_dataset="Bad Name/x"), trial)


def test_a_second_ngrok_account_never_uses_the_stored_domain(env, tmp_path, monkeypatch):
    K.domain_file().write_text("first-account.ngrok-free.dev\n", encoding="utf-8")
    monkeypatch.delenv("NOVEL_NGROK_DOMAIN", raising=False)
    assert K.ngrok_domain(ns()) == "first-account.ngrok-free.dev"                     # the normal case
    assert K.ngrok_domain(ns(ngrok_dataset="ngrok-token-2")) is None                  # another token: no fallback
    assert K.ngrok_domain(ns(ngrok_dataset="ngrok-token-2", domain="second.ngrok-free.dev"), save=False) == "second.ngrok-free.dev"
    assert K.ngrok_domain(ns(ngrok_dataset="ngrok-token-2", domain="second.ngrok-free.dev")) == "second.ngrok-free.dev"
    assert K.domain_file().read_text(encoding="utf-8").strip() == "first-account.ngrok-free.dev"   # not overwritten
    monkeypatch.setenv("NOVEL_NGROK_DOMAIN", "other.ngrok-free.dev")
    assert K.ngrok_domain(ns(ngrok_dataset="ngrok-token-2")) == "other.ngrok-free.dev"


def test_qwen_run_writes_its_own_folder_and_sends_the_place_first(env, tmp_path, fc, capsys):
    trial = make_trial(tmp_path)
    write_server(trial, fc, comfy_user=None, password=None, model=QW)
    fc.user = None
    jpg(trial / "images" / "s01.jpg", seed=9)                       # an earlier FLUX picture
    flux_bytes = (trial / "images" / "s01.jpg").read_bytes()
    assert K.cmd_run(ns(), trial) == 0
    root = trial / kcomfy.QW_DIR
    assert (trial / "images" / "s01.jpg").read_bytes() == flux_bytes and not (trial / kcomfy.MM_DIR).exists()
    gs = graphs(fc)
    assert len(gs) == 6
    for g in gs:
        assert {n["class_type"] for n in g["graph"].values()} >= {"TextEncodeQwenImage21", "SamplerCustomAdvanced"}
    enc = lambda g: node(g, "TextEncodeQwenImage21")
    assert enc(gs[0])["prompt"] == "Place g01, empty."
    # the plate (from the Qwen folder) is image_1; the prompt is renumbered to match
    assert enc(gs[2])["prompt"] == "One person <image2> in <image1>"
    assert load_refs(gs[2]) == ["ktrial-c0024-g01.jpg", "ktrial-lumian_lee.jpg"]
    assert fc.uploads["ktrial-c0024-g01.jpg"] == (root / "plates" / "g01.jpg").read_bytes()
    for rel in ("plates/g01", "plates/g02", "images/s01", "images/s02", "images/s03", "images/s04"):
        assert K._valid_jpeg(root / f"{rel}.jpg")
        meta = read(root / f"{rel}.meta.json")
        assert meta["model"] == kcomfy.QW_UNET_NAME and meta["text_encoder"] == kcomfy.QW_CLIP_NAME and meta["steps"] == 8
        assert (root / f"{rel}.prompt.txt").read_text(encoding="utf-8")
    assert "<image1>" in (root / "images" / "s01.prompt.txt").read_text(encoding="utf-8")
    assert "<ref_image" in (trial / "images" / "s01.prompt.txt").read_text(encoding="utf-8")    # the FLUX file stays
    assert f"pictures go to {root}" in capsys.readouterr().out


def test_a_server_without_a_model_is_flux(env, tmp_path, fc):
    trial = make_trial(tmp_path)
    write_server(trial, fc, comfy_user=None, password=None)
    fc.user = None
    assert K.cmd_run(ns(only="s03"), trial) == 0
    assert (trial / "images" / "s03.jpg").exists() and not (trial / kcomfy.MM_DIR).exists()
    assert node(graphs(fc)[-1], "UNETLoader")["unet_name"] == kcomfy.UNET_NAME
