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


def test_auth_proxy_routes_the_second_card_prefix_to_its_own_comfy():
    with FakeComfy() as one, FakeComfy() as two:
        proxy = kcomfy.serve_auth_proxy(0, one.url, "ktrial", "pw", routes={kcomfy.SECOND_PREFIX: two.url})
        try:
            url = f"http://127.0.0.1:{proxy.server_address[1]}"
            fast(url, user="ktrial", password="pw").ping()
            n_one, n_two = len(one.requests), len(two.requests)
            c2 = fast(url + kcomfy.SECOND_PREFIX, user="ktrial", password="pw")
            name = c2.upload_bytes(kcomfy.tiny_png(), "b.png")
            data, _ = c2.generate("y", 4, 64, 48, [name], "p", timeout=10)
            assert data[:2] == b"\xff\xd8"
            assert len(one.requests) == n_one and len(two.requests) > n_two       # all of it went to the second
            with pytest.raises(ComfyError, match="401"):
                fast(url + kcomfy.SECOND_PREFIX).ping()                            # same password on both cards
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
    assert 'session.get("domain")' in text and 'url="https://" + domain' in text      # the fixed ngrok dev domain
    # the tunnel order: the named Cloudflare tunnel first, then ngrok, then a quick tunnel; a second card behind /g2
    assert 'ORDER = (["named"] if CF_HOST else []) + (["ngrok"] if TUNNEL == "ngrok" else []) + ["quick"]' in text
    assert "routes={kcomfy.SECOND_PREFIX:" in text and "KTRIAL SECOND CARD" in text
    assert 'session.get("second_gpu", True)' in text      # `up` always turns it off: the second T4 never fits in RAM
    # each notebook reads its own tunnel token (the second one: cloudflare-tunnel-2)
    assert '_secret(session.get("cf_dataset", "cloudflare-tunnel"), session.get("cf_file", "CLOUDFLARE_TUNNEL_TOKEN"))' in text
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] != "code":
            continue
        src = "\n".join(l for l in "".join(c["source"]).splitlines() if not l.lstrip().startswith(("!", "%")))
        compile(src, f"cell{i}", "exec")


# ---------------------------------------------------------------- MiniMax-H3-Image (GGUF)

@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_minimax_workflow_shape(n):
    refs = [f"r{i}.jpg" for i in range(n)]
    g = build_workflow("A <Picture 1> test", 77, 1376, 768, refs, "ktrial/s01", model=kcomfy.MINIMAX)
    g = json.loads(json.dumps(g))
    for nid, node in g.items():
        assert "class_type" in node
        for val in node["inputs"].values():
            if isinstance(val, list):
                assert len(val) == 2 and val[0] in g, (nid, val)
    by = {}
    for node in g.values():
        by.setdefault(node["class_type"], []).append(node["inputs"])
    ks = by["KSampler"][0]
    assert ks["seed"] == 77 and ks["steps"] == 6 and ks["cfg"] == 1.0
    assert ks["sampler_name"] == "euler" and ks["scheduler"] == "simple" and ks["denoise"] == 1.0
    cond_id = ks["positive"][0]
    cond = g[cond_id]["inputs"]
    assert g[cond_id]["class_type"] == "MiniMaxH3ImageConditioning"
    assert cond["prompt"] == "A <Picture 1> test" and cond["width"] == 1376 and cond["height"] == 768
    assert ks["latent_image"] == [cond_id, 1] and ks["positive"] == [cond_id, 0]
    assert g[ks["negative"][0]]["class_type"] == "ConditioningZeroOut"           # no negative prompt
    assert g[ks["negative"][0]]["inputs"]["conditioning"] == [cond_id, 0]
    assert by["KTrialUnetLoaderGGUF"][0] == {"unet_name": kcomfy.MM_UNET_NAME, "device": "last_gpu"}
    assert by["KTrialCLIPLoaderGGUF"][0] == {"clip_name": kcomfy.MM_CLIP_NAME, "type": "minimax"}
    assert by["VAELoader"][0]["vae_name"] == kcomfy.MM_VAE_NAME
    assert g[cond["clip"][0]]["class_type"] == "KTrialCLIPLoaderGGUF" and g[cond["vae"][0]]["class_type"] == "VAELoader"
    assert g[ks["model"][0]]["class_type"] == "KTrialUnetLoaderGGUF"
    assert "UNETLoader" not in by and "ReferenceMultiLatent" not in by
    order = [g[cond[f"images.image_{i + 1}"][0]]["inputs"]["image"] for i in range(n)]
    assert order == refs                                  # <Picture i+1> is the i-th connected image
    assert sum(1 for k in cond if k.startswith("images.")) == n
    assert by["SaveImage"][0]["filename_prefix"] == "ktrial/s01"


def test_minimax_size_step_is_32_and_flux_stays_16():
    build_workflow("x", 1, 1376, 768, [], "p", model=kcomfy.MINIMAX)
    with pytest.raises(ValueError, match="32"):
        build_workflow("x", 1, 1360, 768, [], "p", model=kcomfy.MINIMAX)        # a multiple of 16, not of 32
    build_workflow("x", 1, 1360, 768, [], "p")                                  # FLUX: fine
    with pytest.raises(ValueError, match="unknown model"):
        build_workflow("x", 1, 1376, 768, [], "p", model="nope")
    with pytest.raises(ValueError):
        build_workflow("x", 1, 1376, 768, list("abcd"), "p", model=kcomfy.MINIMAX)
    assert build_workflow("x", 1, 64, 64, [], "p") == build_workflow("x", 1, 64, 64, [], "p", model=kcomfy.FLUX)


def test_adapt_prompt_names_pictures_one_based_for_minimax_only():
    t = "<ref_image_0> is Lumian. <ref_image_1> is the place. <ref_image_2> too"
    assert kcomfy.adapt_prompt(kcomfy.MINIMAX, t) == "<Picture 1> is Lumian. <Picture 2> is the place. <Picture 3> too"
    assert kcomfy.adapt_prompt(kcomfy.FLUX, t) == t and kcomfy.adapt_prompt(kcomfy.MINIMAX, "plain") == "plain"


def test_minimax_files_layouts_and_node_source():
    import ast
    plan = kcomfy.download_plan(kcomfy.MINIMAX)
    assert [Path(p).name for _, p, _, _ in plan] == [kcomfy.MM_UNET_NAME, kcomfy.MM_CLIP_NAME, kcomfy.MM_VAE_NAME]
    assert sum(sz for *_, sz in plan) / 1e9 < kcomfy.MM_DOWNLOAD_GB
    assert all(len(c) == 40 for _, _, c in kcomfy.MM_NODES)                         # full commit hashes
    flux = kcomfy.download_plan(kcomfy.FLUX)
    assert [(p, sz) for _, p, _, sz in flux] == list(kcomfy.MODEL_FILES) and {r for r, *_ in flux} == {kcomfy.HF_REPO}
    assert [l["name"] for l in kcomfy.layouts(kcomfy.MINIMAX)] == [l["name"] for l in kcomfy.MM_LAYOUTS]
    assert kcomfy.layouts(kcomfy.MINIMAX, ["--x"])[0]["args"] == ["--x"]
    assert kcomfy.layouts(kcomfy.FLUX, None, "1") == [{"name": "default", "visible": "1", "args": []}]
    assert ast.parse(kcomfy.KTRIAL_NODES_PY).body and "KTrialUnetLoaderGGUF" in kcomfy.KTRIAL_NODES_PY
    assert "KTrialCLIPLoaderGGUF" in kcomfy.KTRIAL_NODES_PY and "model.embed_tokens.weight" in kcomfy.KTRIAL_NODES_PY
    assert kcomfy.model_info(kcomfy.FLUX) == {"model": kcomfy.UNET_NAME, "steps": kcomfy.STEPS}


def test_notebook_handles_both_models():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    text = "".join("".join(c["source"]) for c in nb["cells"])
    for needle in ('session.get("model"', "kcomfy.download_plan(MODEL)", "kcomfy.layouts(MODEL", "KTRIAL_NODES_PY",
                   "kcomfy.MM_NODES", "KTRIAL LAYOUT", 'env.update(layout.get("env") or {})', "KTRIAL DEGRADED"):
        assert needle in text, needle


def test_minimax_vae_layout_is_last_and_graph_unchanged():
    names = [l["name"] for l in kcomfy.MM_LAYOUTS]
    assert names[:2] == ["two-cards-fp32-reserve2", "two-cards-fp32-reserve3"]          # start 4 stays comparable
    last = kcomfy.layouts(kcomfy.MINIMAX)[-1]
    assert last["env"] == {"KTRIAL_VAE_DEVICE": "last_gpu"} and "--fp32-unet" in last["args"]
    assert all("env" not in l for l in kcomfy.MM_LAYOUTS[:2])
    assert kcomfy.layouts(kcomfy.MINIMAX, ["--x"])[-1]["env"] == last["env"]             # args override keeps the env
    assert "env" not in kcomfy.layouts(kcomfy.FLUX)[0]
    g = build_workflow("p", 1, 1376, 768, ["a.png"], "x", model=kcomfy.MINIMAX)
    assert g["3"] == {"class_type": "VAELoader", "inputs": {"vae_name": kcomfy.MM_VAE_NAME}}   # same graph every layout


# ---------------------------------------------------------------- Qwen-Image-2.1-Turbo (INT8)

@pytest.mark.parametrize("n", [0, 1, 3])
def test_qwen_workflow_shape(n):
    refs = [f"r{i}.jpg" for i in range(n)]
    g = json.loads(json.dumps(build_workflow("A <image1> test", 77, 1376, 768, refs, "ktrial/s01", model=kcomfy.QWEN)))
    for nid, node in g.items():
        assert "class_type" in node
        for val in node["inputs"].values():
            if isinstance(val, list):
                assert len(val) == 2 and val[0] in g, (nid, val)
    by = {}
    for node in g.values():
        by.setdefault(node["class_type"], []).append(node["inputs"])
    sa = by["SamplerCustomAdvanced"][0]
    assert by["RandomNoise"][0] == {"noise_seed": 77} and by["KSamplerSelect"][0] == {"sampler_name": "euler"}
    assert len(by["ManualSigmas"][0]["sigmas"].split(",")) == kcomfy.QW_STEPS + 1 and by["ManualSigmas"][0]["sigmas"].endswith(", 0")
    guider = g[sa["guider"][0]]
    assert guider["class_type"] == "CFGGuider" and guider["inputs"]["cfg"] == 1.0
    enc_id = guider["inputs"]["positive"][0]
    enc = g[enc_id]["inputs"]
    assert g[enc_id]["class_type"] == "TextEncodeQwenImage21" and guider["inputs"]["negative"] == [enc_id, 1]
    assert enc["prompt"] == "A <image1> test" and enc["negative_prompt"] == "" and enc["resolution"] == kcomfy.QW_REF_RESOLUTION
    assert g[enc["clip"][0]]["inputs"] == {"clip_name": kcomfy.QW_CLIP_NAME, "type": "qwen_image", "device": "default"}
    assert g[guider["inputs"]["model"][0]]["inputs"]["unet_name"] == kcomfy.QW_UNET_NAME
    assert by["VAELoader"][0]["vae_name"] == kcomfy.QW_VAE_NAME
    lat = g[sa["latent_image"][0]]
    assert lat["class_type"] == "EmptyLatentImage" and (lat["inputs"]["width"], lat["inputs"]["height"]) == (1376, 768)
    order = [g[enc[f"images.image_{i + 1}"][0]]["inputs"]["image"] for i in range(n)]
    assert order == refs and sum(1 for k in enc if k.startswith("images.")) == n          # <image i+1> is the i-th image
    assert ("vae" in enc) == bool(n)                                                       # references also go in as latents
    assert "KSampler" not in by and "KTrialUnetLoaderGGUF" not in by and "ReferenceMultiLatent" not in by
    assert by["SaveImage"][0]["filename_prefix"] == "ktrial/s01"


def test_qwen_size_step_is_32():
    build_workflow("x", 1, 1376, 768, [], "p", model=kcomfy.QWEN)
    with pytest.raises(ValueError, match="32"):
        build_workflow("x", 1, 1360, 768, [], "p", model=kcomfy.QWEN)                   # a multiple of 16, not of 32
    with pytest.raises(ValueError):
        build_workflow("x", 1, 1376, 768, list("abcd"), "p", model=kcomfy.QWEN)


def test_qwen_prompt_wording_and_the_place_goes_first():
    t = "<ref_image_0> is A. <ref_image_1> is B. <ref_image_2> is the place. A (<ref_image_0>) left of <ref_image_1>"
    assert kcomfy.adapt_prompt(kcomfy.QWEN, t) == (
        "<image1> is A. <image2> is B. <image3> is the place. A (<image1>) left of <image2>")
    assert kcomfy.adapt_prompt(kcomfy.QWEN, "plain") == "plain"
    refs = [{"role": "character", "file": "a"}, {"role": "character", "file": "b"}, {"role": "place", "file": "p"}]
    text, out = kcomfy.qwen_order(t, refs)
    assert [r["file"] for r in out] == ["p", "a", "b"]
    assert text == "<ref_image_1> is A. <ref_image_2> is B. <ref_image_0> is the place. A (<ref_image_1>) left of <ref_image_2>"
    assert kcomfy.adapt_prompt(kcomfy.QWEN, text).startswith("<image2> is A. <image3> is B. <image1> is the place.")
    two = "<ref_image_0> is A. <ref_image_1> is B."
    assert kcomfy.qwen_order(two, refs[:2]) == (two, refs[:2])                             # no place: unchanged
    assert kcomfy.qwen_order("x", []) == ("x", [])


def test_qwen_files_layouts_and_helpers():
    plan = kcomfy.download_plan(kcomfy.QWEN)
    assert [Path(p).name for _, p, _, _ in plan] == [kcomfy.QW_UNET_NAME, kcomfy.QW_CLIP_NAME, kcomfy.QW_VAE_NAME]
    assert all(str(Path(p).parent) == folder for _, p, folder, _ in plan)       # the notebook downloads straight into models/
    assert sum(sz for *_, sz in plan) / 1e9 < kcomfy.QW_DOWNLOAD_GB
    assert [l["name"] for l in kcomfy.layouts(kcomfy.QWEN, None, "1")] == [l["name"] for l in kcomfy.QW_LAYOUTS]
    assert {l["visible"] for l in kcomfy.layouts(kcomfy.QWEN, None, "1")} == {"1"}
    assert kcomfy.layouts(kcomfy.QWEN, ["--x"])[0]["args"] == ["--x"] and kcomfy.layouts(kcomfy.QWEN)[1]["args"] == ["--fp32-unet"]
    assert kcomfy.model_info(kcomfy.QWEN) == {"model": kcomfy.QW_UNET_NAME, "text_encoder": kcomfy.QW_CLIP_NAME, "steps": 8}
    assert (kcomfy.download_gb(kcomfy.FLUX), kcomfy.download_gb(kcomfy.MINIMAX)) == (0, kcomfy.MM_DOWNLOAD_GB)
    assert kcomfy.picture_timeout(kcomfy.QWEN) == kcomfy.QW_TIMEOUT and kcomfy.picture_timeout(kcomfy.FLUX) == 600.0
    assert kcomfy.probe_timeout(kcomfy.QWEN) == kcomfy.QW_PROBE_TIMEOUT and kcomfy.probe_timeout(kcomfy.FLUX) == 1800.0


def test_notebook_handles_qwen_and_a_second_token_dataset():
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    text = "".join("".join(c["source"]) for c in nb["cells"])
    for needle in ('session.get("ngrok_dataset", "ngrok-token")', "kcomfy.download_gb(MODEL)", "kcomfy.probe_timeout(MODEL)",
                   "MODEL != kcomfy.FLUX", "MODEL == kcomfy.FLUX"):
        assert needle in text, needle
    assert "MM_DOWNLOAD_GB" not in text and "MM_PROBE_TIMEOUT" not in text           # no MiniMax-only constants left


def _exec_nodes(monkeypatch, env: str | None, cuda_count: int = 2):
    """Run the ktrial_nodes.py text against stub torch / comfy / folder_paths / nodes modules."""
    import types
    made = []

    class Dev:
        def __init__(self, kind, idx=None):
            self.kind, self.idx = kind, idx

        def __repr__(self):
            return f"{self.kind}:{self.idx}"

    torch = types.SimpleNamespace(device=Dev, float16="f16", cuda=types.SimpleNamespace(
        is_available=lambda: cuda_count > 0, device_count=lambda: cuda_count))

    class VAE:
        def __init__(self, sd=None, device=None, metadata=None):
            made.append({"sd": sd, "device": device, "metadata": metadata})

        def throw_exception_if_invalid(self):
            pass

    class StockVAELoader:
        def load_vae(self, vae_name):
            return ("stock", vae_name)

    comfy = types.ModuleType("comfy")
    comfy.sd = types.SimpleNamespace(VAE=VAE, load_diffusion_model_state_dict=lambda *a, **k: None)
    comfy.utils = types.SimpleNamespace(load_torch_file=lambda p, return_metadata=False: ({"w": p}, {"m": 1}))
    fp = types.SimpleNamespace(get_filename_list=lambda f: ["vae.safetensors"] if f == "vae" else [],
                               get_full_path_or_raise=lambda f, n: f"/models/{f}/{n}")
    nodes = types.SimpleNamespace(VAELoader=StockVAELoader, NODE_CLASS_MAPPINGS={})
    for name, mod in {"torch": torch, "comfy": comfy, "comfy.sd": comfy.sd, "comfy.utils": comfy.utils,
                      "folder_paths": fp, "nodes": nodes}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    if env is None:
        monkeypatch.delenv("KTRIAL_VAE_DEVICE", raising=False)
    else:
        monkeypatch.setenv("KTRIAL_VAE_DEVICE", env)
    ns: dict = {}
    exec(compile(kcomfy.KTRIAL_NODES_PY, "ktrial_nodes.py", "exec"), ns)
    return ns, nodes, made


def test_ktrial_vae_loader_moves_vae_only_when_the_layout_asks(monkeypatch):
    ns, nodes, made = _exec_nodes(monkeypatch, None)
    assert "KTrialVAELoader" in ns["NODE_CLASS_MAPPINGS"]
    assert nodes.VAELoader().load_vae("vae.safetensors") == ("stock", "vae.safetensors") and not made

    ns, nodes, made = _exec_nodes(monkeypatch, "last_gpu")
    out = nodes.VAELoader().load_vae("vae.safetensors")
    assert len(out) == 1 and made[-1]["sd"] == {"w": "/models/vae/vae.safetensors"} and made[-1]["metadata"] == {"m": 1}
    assert repr(made[-1]["device"]) == "cuda:1"
    assert nodes.VAELoader().load_vae("pixel_space") == ("stock", "pixel_space")      # special names stay stock

    ns, nodes, made = _exec_nodes(monkeypatch, "last_gpu", cuda_count=1)
    nodes.VAELoader().load_vae("vae.safetensors")
    assert made[-1]["device"] is None                                                   # one card: ComfyUI's default


def test_qwen_split_encoder_layout_comes_first_only_when_asked():
    plain = kcomfy.layouts(kcomfy.QWEN, None, flux_visible="1")
    assert [l["name"] for l in plain] == ["one-card-default", "one-card-fp32-unet"] and plain[0]["visible"] == "1"
    split = kcomfy.layouts(kcomfy.QWEN, None, flux_visible="1", split_encoder=True)
    assert split[0]["name"] == "two-cards-encoder-card1" and split[0]["visible"] == ""     # both cards
    assert split[0]["env"] == {"KTRIAL_CLIP_DEVICE": "last_gpu"} and split[1:] == plain     # the fallbacks stay


def test_the_node_file_compiles_and_the_notebook_writes_it_for_qwen():
    compile(kcomfy.KTRIAL_NODES_PY, "ktrial_nodes.py", "exec")
    assert 'os.environ.get("KTRIAL_CLIP_DEVICE") == "last_gpu"' in kcomfy.KTRIAL_NODES_PY
    import json as _json
    nb = _json.loads(Path(__file__).resolve().parents[1].joinpath("kaggle", "comfy_server.ipynb").read_text(
        encoding="utf-8"))
    src = "".join("".join(c["source"]) for c in nb["cells"])
    assert "if MODEL == kcomfy.QWEN:" in src and 'split_encoder=bool(session.get("split_encoder"))' in src
    assert "ktrial_layout.json" in src
