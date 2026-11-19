"""novel kllm: up / status / url / down against the fake Kaggle CLI, a fake llama-server behind the real proxy, and a
fake ntfy. The "kernel" is a thread that waits for the staged session dataset, reads the key and topic from it (as the
notebook does), starts the proxy and posts its address to the relay. Nothing real is called."""

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from novel import kllm
from novel.commands import kllm_cmd
from novel.stages import kllm_run as K

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_llm import FakeLlama, FakeNtfy  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FAKE = REPO / "tests" / "fakes" / "fake_kaggle.py"
NOTEBOOK = REPO / "kaggle" / "llm_server.ipynb"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, khome, state = (tmp_path / n for n in ("home", "kaggle-home", "fake-state"))
    for d in (home, khome, state):
        d.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({
        "data_dir": str(tmp_path),
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
              "FAKE_KAGGLE_EMPTY_LOG_WHILE_RUNNING", "NOVEL_KLLM_URL_OVERRIDE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOVEL_TTS_UPLOAD_WAIT", "0")
    monkeypatch.setenv("NOVEL_KLLM_POLL", "0")
    monkeypatch.setenv("NOVEL_KLLM_WAIT_MINUTES", "0.05")
    monkeypatch.setenv("NOVEL_KLLM_PROBES", "3")
    monkeypatch.setenv("NOVEL_KLLM_DOWN_MINUTES", "0.05")
    pi = tmp_path / "pi-models.json"
    monkeypatch.setenv("NOVEL_PI_MODELS", str(pi))
    ntfy = FakeNtfy()
    monkeypatch.setenv("NOVEL_KLLM_NTFY_URL", ntfy.url)
    stack = {"threads": [], "closers": [ntfy.close]}
    yield SimpleNamespace(state=state, calls=calls, pi=pi, ntfy=ntfy, stack=stack, home=home)
    for c in stack["closers"]:
        c()


def args(**kw):
    base = dict(account=None, idle_minutes=None, max_hours=None, context=131072, thinking="off", pad_kb=None,
                smoke=False, dry_run=False, no_wait=False)
    base.update(kw)
    return SimpleNamespace(**base)


def fake_kernel(env, post=True):
    """What the notebook does once it runs: read session.json, serve the proxy on that key, post the address."""
    out = {}
    session_file = env.state / "datasets" / "fakeuser__kllm-session" / "session.json"

    def run():
        end = time.time() + 20
        while time.time() < end and not session_file.exists():
            time.sleep(0.05)
        session = json.loads(session_file.read_text(encoding="utf-8"))
        up = FakeLlama(events=3, delay=0.01)
        state = kllm.ProxyState(idle_minutes=session["idle_minutes"], pad=0,
                                info={"ctx": session["contexts"][0], "model": session["alias"]})
        px = kllm.serve_llm_proxy(up.port, session["key"], state)
        env.stack["closers"] += [px.shutdown, up.close]
        out.update(session=session, state=state, px=px)
        if post:
            kllm.relay_post(session["topic"], {"url": f"http://127.0.0.1:{px.server_address[1]}",
                                               "ctx": session["contexts"][0], "model": session["alias"], "ready_s": 90})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    env.stack["threads"].append(t)
    return out, t


def calls_text(env) -> str:
    return env.calls.read_text(encoding="utf-8") if env.calls.exists() else ""


# ---------------------------------------------------------------- up

def test_dry_run_stages_and_calls_nothing(env, capsys):
    assert K.cmd_up(args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "kllm.py" in out and "session.json" in out
    assert "kernels push" not in calls_text(env) and "datasets create" not in calls_text(env)
    sess = json.loads((K.state_dir() / "kaggle" / "dataset" / "session.json").read_text(encoding="utf-8"))
    assert sess["contexts"] == [131072, 65536, 32768] and sess["idle_minutes"] == 30 and sess["max_hours"] == 11.5
    assert len(sess["key"]) >= 32 and len(sess["topic"]) == 32 and sess["pad_bytes"] == kllm.DEFAULT_PAD
    meta = json.loads((K.state_dir() / "kaggle" / "kernel" / "kernel-metadata.json").read_text(encoding="utf-8"))
    assert meta["enable_gpu"] and meta["machine_shape"] == "NvidiaTeslaT4" and meta["id"] == "fakeuser/kllm-server"
    assert meta["dataset_sources"] == ["fakeuser/kllm-session", "fakeuser/hf-token"]
    assert sess["key"] not in out                                    # the key is never printed
    assert not K.load_server()


def test_context_ladder_follows_the_start_size():
    assert K._ctx_ladder(131072) == [131072, 65536, 32768]
    assert K._ctx_ladder(65536) == [65536, 32768]
    assert K._ctx_ladder(40000) == [40000, 32768]
    assert K._ctx_ladder(4096) == [4096]


def test_smoke_kernel_is_cpu_and_pi_is_not_touched(env, capsys):
    out, t = fake_kernel(env)
    assert K.cmd_up(args(smoke=True)) == 0
    meta = json.loads((K.state_dir() / "kaggle" / "kernel" / "kernel-metadata.json").read_text(encoding="utf-8"))
    assert meta["enable_gpu"] is False and "machine_shape" not in meta
    assert out["session"]["smoke"] is True
    assert not env.pi.exists()


def test_up_end_to_end_then_status_down(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "2")
    env.pi.write_text(json.dumps({"providers": {"ollama": {"baseUrl": "http://localhost:11434/v1", "models": [{"id": "x"}]}}}))
    out, t = fake_kernel(env)
    assert K.cmd_up(args(idle_minutes=2, max_hours=1, thinking="on")) == 0
    text = capsys.readouterr().out
    key = out["session"]["key"]
    assert "the server answers at http://127.0.0.1:" in text and "pi --model kaggle/qwen-local" in text
    assert key not in text
    assert out["session"]["idle_minutes"] == 2 and out["session"]["thinking"] is True
    assert all(key not in m for msgs in env.ntfy.topics.values() for m in msgs)      # the relay never sees the key
    server = K.load_server()
    assert server["url"].startswith("http://127.0.0.1:") and server["key"] == key and server["kernel"] == "fakeuser/kllm-server"
    assert server["ready_after_s"] >= 0 and server["ctx"] == 131072
    pi = json.loads(env.pi.read_text(encoding="utf-8"))["providers"]
    assert pi["ollama"]["models"] == [{"id": "x"}]                  # the other provider is untouched
    k = pi["kaggle"]
    assert k["baseUrl"] == server["url"] + "/v1" and k["apiKey"] == key and k["api"] == "openai-completions"
    assert k["models"][0]["id"] == "qwen-local" and k["models"][0]["contextWindow"] == 131072 and k["models"][0]["reasoning"] is True
    assert "kernels push" in calls_text(env)

    # a second `up` while the kernel still runs is refused (FAKE polls count per status call)
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "1000")
    assert K.cmd_up(args()) == 1
    assert "still RUNNING" in capsys.readouterr().out

    assert K.cmd_status(args()) == 0
    status = capsys.readouterr().out
    assert "the server answers: context 131072" in status and "idle 0 of 2 min" in status and "RUNNING" in status

    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "0")             # the "kernel" ends once it has been told to stop
    assert K.cmd_down(args()) == 0
    down = capsys.readouterr().out
    assert "stop requested" in down and "the kernel has ended" in down
    assert out["state"].stop_event.is_set() and K.load_server()["stopped"]


def test_up_reports_a_kernel_that_ended_without_an_address(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_ERROR", "KLLM FATAL: no scratch directory")
    assert K.cmd_up(args()) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "never posted an address" in out and "KLLM FATAL: no scratch directory" in out


def test_up_times_out_with_the_manual_hint(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_RUNNING_POLLS", "100000")
    monkeypatch.setenv("NOVEL_KLLM_WAIT_MINUTES", "0.01")
    assert K.cmd_up(args()) == 1
    assert "novel kllm url ADDRESS" in capsys.readouterr().out


def test_no_wait_returns_after_the_push(env, capsys):
    assert K.cmd_up(args(no_wait=True)) == 0
    assert K.load_server()["url"] is None and "pushed" in capsys.readouterr().out


def test_url_override_skips_the_relay(env, capsys, monkeypatch):
    out, t = fake_kernel(env, post=False)

    def late_override():
        end = time.time() + 20
        while time.time() < end and "px" not in out:
            time.sleep(0.05)
        monkeypatch.setenv("NOVEL_KLLM_URL_OVERRIDE", f"http://127.0.0.1:{out['px'].server_address[1]}")
    threading.Thread(target=late_override, daemon=True).start()
    monkeypatch.setenv("NOVEL_KLLM_WAIT_MINUTES", "0.3")
    assert K.cmd_up(args()) == 0
    assert "the server answers" in capsys.readouterr().out


# ---------------------------------------------------------------- url, status without a server, down failures

def test_status_and_down_without_a_server(env, capsys):
    assert K.cmd_status(args()) == 1 and "no server was started" in capsys.readouterr().out
    assert K.cmd_down(args()) == 0
    assert K.cmd_url(SimpleNamespace(address="https://a.trycloudflare.com")) == 1


def test_url_stores_and_checks(env, capsys):
    out, t = fake_kernel(env, post=False)
    assert K.cmd_up(args(no_wait=True)) == 0
    t.join(10)
    capsys.readouterr()
    assert K.cmd_url(SimpleNamespace(address="not an address")) == 1
    assert "is not an address" in capsys.readouterr().out
    addr = f"http://127.0.0.1:{out['px'].server_address[1]}/"
    assert K.cmd_url(SimpleNamespace(address=addr)) == 0
    assert K.load_server()["url"] == addr.rstrip("/")
    assert json.loads(env.pi.read_text(encoding="utf-8"))["providers"]["kaggle"]["baseUrl"].endswith("/v1")
    assert K.cmd_url(SimpleNamespace(address="http://127.0.0.1:1")) == 1       # stored, but nothing answers
    assert "does not answer yet" in capsys.readouterr().out


def test_down_when_the_server_is_gone(env, capsys):
    assert K.cmd_up(args(no_wait=True)) == 0
    s = K.load_server()
    s["url"] = "http://127.0.0.1:1"
    K.save_server(s)
    assert K.cmd_down(args()) == 0
    assert "probably stopped already" in capsys.readouterr().out and K.load_server()["stopped"]


def test_clean_url():
    assert K.clean_url("https://x-1.trycloudflare.com/") == "https://x-1.trycloudflare.com"
    assert K.clean_url("x-1.trycloudflare.com") == "https://x-1.trycloudflare.com"
    for bad in ("", "https://a b.com", "http://example.com", "ftp://x.y", "https://x/path"):
        with pytest.raises(K.KllmRunError):
            K.clean_url(bad)


# ---------------------------------------------------------------- Pi's models.json

def test_merge_pi_config(tmp_path):
    f = tmp_path / "agent" / "models.json"
    assert K.merge_pi_config("https://a.trycloudflare.com", "k" * 32, 65536, False, f) == f
    data = json.loads(f.read_text(encoding="utf-8"))
    m = data["providers"]["kaggle"]["models"][0]
    assert m["contextWindow"] == 65536 and m["maxTokens"] == 16384 and m["reasoning"] is False
    data["$schema"] = "s"
    data["providers"]["other"] = {"x": 1}
    f.write_text(json.dumps(data), encoding="utf-8")
    K.merge_pi_config("https://b.trycloudflare.com", "j" * 32, 32768, True, f)
    again = json.loads(f.read_text(encoding="utf-8"))
    assert again["$schema"] == "s" and again["providers"]["other"] == {"x": 1}
    assert again["providers"]["kaggle"]["baseUrl"] == "https://b.trycloudflare.com/v1"
    f.write_text("{ not json", encoding="utf-8")
    assert K.merge_pi_config("https://c.trycloudflare.com", "k" * 32, 1, False, f) is None
    assert f.read_text(encoding="utf-8") == "{ not json"


# ---------------------------------------------------------------- the command and the notebook

def test_command_help_and_unknown(capsys):
    assert kllm_cmd.main([]) == 0 and "novel kllm up" in capsys.readouterr().out
    assert kllm_cmd.main(["-h"]) == 0
    assert kllm_cmd.main(["nope"]) == 2


def test_notebook_cells_compile_and_never_print_the_key():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    kinds = [c["cell_type"] for c in nb["cells"]]
    assert kinds[0] == "markdown" and set(kinds[1:]) == {"code"}
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] == "code":
            src = "\n".join(l for l in "".join(c["source"]).splitlines() if not l.lstrip().startswith(("!", "%")))
            compile(src, f"cell{i}", "exec")
    text = "".join("".join(c["source"]) for c in nb["cells"])
    for needle in ("_find_dataset('kllm-session')", "_secret('hf-token', 'HF_TOKEN')", "KLLM URL", "kllm_summary.json",
                   "serve_llm_proxy", "relay_post", "os._exit(3)", "contexts"):
        assert needle in text, needle
    assert "relay_post(TOPIC, msg)" in text and '"key"' not in text.split("def publish")[1].split("TUNNEL = URL")[0]
    assert "--api-key" not in text and "print(KEY" not in text and "KEY}" not in text
