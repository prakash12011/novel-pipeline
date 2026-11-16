"""novel.kcomfy: the FLUX.3 [klein] graph, the ComfyUI client against tests/fakes/fake_comfy.py, the auth proxy,
and a structural check of kaggle/comfy_server.ipynb."""

import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from novel import kcomfy
from novel.kcomfy import Comfy, ComfyError, build_workflow

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_comfy import FakeComfy  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NOTEBOOK = REPO / "kaggle" / "comfy_server.ipynb"


def fast(url, **kw):
    kw.setdefault("backoff", 0.01)
    kw.setdefault("poll", 0.05)
    return Comfy(url, **kw)


# ---------------------------------------------------------------- build_workflow

@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_workflow_shape(n):
    refs = [f"r{i}.jpg" for i in range(n)]
    g = build_workflow("A <ref_image_0> test", 1234567, 1376, 768, refs, "ktrial/s01")
    g = json.loads(json.dumps(g))                       # valid JSON round trip
    for nid, node in g.items():
        assert "class_type" in node
        for val in node["inputs"].values():
            if isinstance(val, list):
                assert len(val) == 2 and val[0] in g, (nid, val)
    by = {}
    for node in g.values():
        by.setdefault(node["class_type"], []).append(node["inputs"])
    ks = by["KSampler"][0]
    assert ks["seed"] == 1234567 and ks["steps"] == kcomfy.STEPS and ks["cfg"] == kcomfy.CFG
    assert ks["sampler_name"] == "euler" and ks["scheduler"] == "simple"
    assert by["EmptyFlux2LatentImage"][0]["width"] == 1376 and by["EmptyFlux2LatentImage"][0]["height"] == 768
    assert g[ks["latent_image"][0]]["class_type"] == "EmptyFlux2LatentImage"
    assert by["SaveImage"][0]["filename_prefix"] == "ktrial/s01"
    texts = [i["text"] for i in by["CLIPTextEncode"]]
    assert "A <ref_image_0> test" in texts
    assert by["UNETLoader"][0]["unet_name"] == kcomfy.UNET_NAME
    assert by["CLIPLoader"][0]["type"] == "flux3"
    assert sorted(i["image"] for i in by.get("LoadImage", [])) == sorted(refs)
    if n:
        multi = by["ReferenceMultiLatent"][0]
        assert multi["resolution"] == "resize" and multi["resolution.megapixels"] == kcomfy.REF_MEGAPIXELS
        order = [g[multi[f"images.image{i}"][0]]["inputs"]["image"] for i in range(n)]
        assert order == refs                             # <ref_image_i> is the i-th connected image
        assert g[ks["positive"][0]]["class_type"] == "ReferenceMultiLatent"
        assert ks["positive"] == [ks["positive"][0], 0] and ks["negative"] == [ks["positive"][0], 1]
        assert "ConditioningZeroOut" in by
    else:
        assert "ReferenceMultiLatent" not in by and "LoadImage" not in by


def test_workflow_rejects_four_refs_and_bad_size():
    with pytest.raises(ValueError):
        build_workflow("x", 1, 1376, 768, ["a", "b", "c", "d"], "p")
    with pytest.raises(ValueError):
        build_workflow("x", 1, 1370, 768, [], "p")


def test_ref_token():
    assert kcomfy.ref_token(0) == "<ref_image_0>" and kcomfy.ref_token(2) == "<ref_image_2>"


# ---------------------------------------------------------------- client against the fake

def test_generate_round_trip(tmp_path):
    with FakeComfy(pending_polls=2) as fc:
        c = fast(fc.url)
        assert c.ping()["system"]["comfyui_version"] == "fake"
        ref = tmp_path / "lumian.jpg"
        Image.new("RGB", (64, 64), "red").save(ref)
        name = c.upload_image(ref, "lumian.jpg")
        assert name == "lumian.jpg" and fc.uploads["lumian.jpg"] == ref.read_bytes()
        data, took = c.generate("hello <ref_image_0>", 7, 1376, 768, [name], "ktrial/s01", timeout=10)
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    assert Image.open(io.BytesIO(data)).size == (1376, 768)
    assert took >= 0
    assert fc.outputs["ktrial/s01_00001_.jpg"] == data
    assert any(p.startswith("/view?") for p in fc.paths("GET"))


def test_unknown_reference_is_rejected():
    with FakeComfy() as fc:
        with pytest.raises(ComfyError, match="Invalid image file"):
            fast(fc.url).generate("x", 1, 64, 64, ["never-uploaded.jpg"], "p", timeout=5)


def test_auth_header_sent_and_401_raises():
    with FakeComfy(user="ktrial", password="s3cret-pass") as fc:
        fast(fc.url, user="ktrial", password="s3cret-pass").ping()
        assert fc.requests[-1]["headers"]["Authorization"].startswith("Basic ")
        with pytest.raises(ComfyError, match="401"):
            fast(fc.url, user="ktrial", password="wrong").ping()
        with pytest.raises(ComfyError, match="401"):
            fast(fc.url).ping()


def test_retries_survive_two_503s():
    with FakeComfy(fail_first=2) as fc:
        data, _ = fast(fc.url).generate("x", 1, 64, 64, [], "p", timeout=10)
        assert data[:2] == b"\xff\xd8"
        assert fc.paths()[:3] == ["/prompt"] * 3


def test_retries_give_up():
    with FakeComfy(fail_first=100) as fc:
        with pytest.raises(ComfyError, match="gave up"):
            fast(fc.url, retries=2).ping()


def test_rejected_graph_carries_node_error_text():
    with FakeComfy(reject=True) as fc:
        with pytest.raises(ComfyError, match=r"UNETLoader.*Value not in list.*missing\.safetensors"):
            fast(fc.url).queue(build_workflow("x", 1, 64, 64, [], "p"))


def test_execution_error_raises():
    with FakeComfy(exec_error=True) as fc:
        with pytest.raises(ComfyError, match="CUDA out of memory"):
            fast(fc.url).generate("x", 1, 64, 64, [], "p", timeout=10)


def test_wait_times_out():
    with FakeComfy(pending_polls=10**6) as fc:
        c = fast(fc.url)
        pid = c.queue(build_workflow("x", 1, 64, 64, [], "p"))
        with pytest.raises(ComfyError, match="not done"):
            c.wait(pid, timeout=0.2, poll=0.05)


def test_request_stop_uploads_a_valid_png():
    with FakeComfy() as fc:
        kcomfy.request_stop(fast(fc.url))
        data = fc.uploads[kcomfy.STOP_NAME]
    assert kcomfy.STOP_NAME == "KTRIAL_STOP.png"
    img = Image.open(io.BytesIO(data))
    img.load()
    assert img.size == (1, 1)


def test_auth_proxy_in_front_of_comfy():
    with FakeComfy() as fc:
        proxy = kcomfy.serve_auth_proxy(0, fc.url, "ktrial", "s3cret-pass")
        try:
            url = f"http://127.0.0.1:{proxy.server_address[1]}"
            with pytest.raises(ComfyError, match="401"):
                fast(url).ping()
            with pytest.raises(ComfyError, match="401"):
                fast(url, user="ktrial", password="nope").ping()
            c = fast(url, user="ktrial", password="s3cret-pass")
            name = c.upload_bytes(kcomfy.tiny_png(), "a.png")
            data, _ = c.generate("x", 3, 64, 48, [name], "p", timeout=10)
            assert data[:2] == b"\xff\xd8"
            assert "Authorization" not in fc.requests[-1]["headers"]      # the password stops at the proxy
        finally:
            proxy.shutdown()


# ---------------------------------------------------------------- the notebook

def test_notebook_structure():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    kinds = [c["cell_type"] for c in nb["cells"]]
    assert kinds[0] == "markdown" and set(kinds[1:]) == {"code"}
    text = "".join("".join(c["source"]) for c in nb["cells"])
    assert "@DATASET@" in text and "KTRIAL URL" in text and "_find_dataset" in text
    assert 'session.get("domain")' in text and 'url="https://" + DOMAIN' in text      # the fixed ngrok dev domain
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] != "code":
            continue
        src = "\n".join(l for l in "".join(c["source"]).splitlines() if not l.lstrip().startswith(("!", "%")))
        compile(src, f"cell{i}", "exec")
