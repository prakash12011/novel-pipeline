"""novel.korouter against a fake OpenRouter image API (tests/fakes/fake_openrouter.py). No real call is ever made."""

import base64
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from novel import korouter
from novel.commands import ktrial_cmd
from novel.korouter import OpenRouter, RouterError

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_openrouter import FakeOpenRouter, make_image  # noqa: E402

KEY = "sk-or-test-key-123456"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(korouter, "_sleep", waits.append)
    return waits


@pytest.fixture
def fo():
    with FakeOpenRouter(key=KEY) as f:
        yield f


def client(fo, **kw):
    return OpenRouter(KEY, fo.url, **kw)


def ref_bytes(color, size=(48, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def decode_ref(entry):
    assert entry["type"] == "image_url"
    url = entry["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert im.format == "JPEG"
    return im.convert("RGB").getpixel((0, 0))


# ---------------------------------------------------------------- hosted_text

def test_hosted_text_turns_comfy_references_into_one_based_image_words():
    assert korouter.hosted_text("<ref_image_0> and <ref_image_1>") == "image 1 and image 2"
    assert korouter.hosted_text("The person in <ref_image_9>, <ref_image_10>.") == "The person in image 10, image 11."
    assert korouter.hosted_text("no references here <b>") == "no references here <b>"
    assert korouter.hosted_text("") == ""


# ---------------------------------------------------------------- request shape

def test_request_shape_and_reference_order(fo):
    red, green = (250, 0, 0), (0, 250, 0)
    data, secs, info = client(fo).generate("Draw image 1 next to image 2", [ref_bytes(red), ref_bytes(green)],
                                           resolution="2K", seed=77)
    assert len(fo.requests) == 1
    req = fo.requests[0]
    assert req["path"] == "/images"
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert req["headers"]["Content-Type"] == "application/json"
    body = req["body"]
    assert body["model"] == korouter.MODEL
    assert body["prompt"] == "Draw image 1 next to image 2"
    assert body["n"] == 1 and body["aspect_ratio"] == "16:9" and body["resolution"] == "2K"
    assert body["seed"] == 77 and body["output_format"] == "png"
    assert set(body) == {"model", "prompt", "n", "aspect_ratio", "resolution", "output_format", "seed",
                         "input_references"}
    refs = body["input_references"]
    assert len(refs) == 2
    r1, r2 = decode_ref(refs[0]), decode_ref(refs[1])
    assert r1[0] > 200 > r1[1] and r2[1] > 200 > r2[0]                    # red first, green second
    assert Image.open(io.BytesIO(data)).size == (320, 192)
    assert secs >= 0 and info["cost"] == 0.009 and (info["width"], info["height"]) == (320, 192)
    assert info["media_type"] == "image/png" and info["bytes"] == len(data)


def test_no_seed_no_references_means_no_such_keys(fo):
    client(fo).generate("plain", [])
    body = fo.requests[0]["body"]
    assert "seed" not in body and "input_references" not in body
    assert body["resolution"] == "1K" and body["aspect_ratio"] == "16:9"


def test_references_may_be_paths_and_any_image_format(fo, tmp_path):
    p = tmp_path / "ref.jpg"
    Image.new("RGB", (40, 40), (0, 0, 250)).save(p, "WEBP")               # a WebP named .jpg
    client(fo).generate("x", [p])
    blue = decode_ref(fo.requests[0]["body"]["input_references"][0])
    assert blue[2] > 200 > blue[0]


def test_unreadable_reference_and_too_many_references_are_errors(fo, tmp_path):
    with pytest.raises(RouterError, match="cannot be read"):
        client(fo).generate("x", [tmp_path / "missing.jpg"])
    with pytest.raises(RouterError, match="at most 10"):
        client(fo).generate("x", [ref_bytes((1, 1, 1))] * 11)
    assert fo.requests == []


def test_cost_is_none_when_the_answer_has_none(fo):
    fo.cost = None
    _, _, info = client(fo).generate("x", [])
    assert info["cost"] is None and info["usage"] == {}
    assert korouter.COST_PER_IMAGE == 0.014
    assert korouter.estimate("1K") == 0.009 and korouter.estimate("2K") == 0.014 and korouter.estimate("weird") == 0.014


# ---------------------------------------------------------------- responses

def test_base64_and_url_responses_both_work(fo):
    d1, _, _ = client(fo).generate("x", [])
    fo.mode = "url"
    d2, _, info = client(fo).generate("x", [])
    assert Image.open(io.BytesIO(d1)).size == Image.open(io.BytesIO(d2)).size == (320, 192)
    assert len(fo.downloads) == 1 and info["width"] == 320
    assert "Authorization" not in fo.downloads[0]["headers"]               # the key never goes to the picture link


def test_a_data_url_in_the_url_field_works(fo):
    fo.script = [(200, {"data": [{"url": "data:image/png;base64," + base64.b64encode(make_image(16, 16)).decode()}]})]
    data, _, info = client(fo).generate("x", [])
    assert Image.open(io.BytesIO(data)).size == (16, 16) and info["cost"] is None


@pytest.mark.parametrize("answer", [{"data": []}, {"data": [{}]}, {"nothing": 1}, {"data": [{"b64_json": "!!!"}]}])
def test_an_answer_without_a_picture_is_an_error(fo, answer):
    fo.script = [(200, answer)]
    with pytest.raises(RouterError, match="no picture"):
        client(fo).generate("x", [])


def test_an_answer_that_is_not_json_is_an_error(fo):
    fo.script = [(200, "<html>oops</html>")]
    with pytest.raises(RouterError, match="not JSON"):
        client(fo).generate("x", [])


# ---------------------------------------------------------------- retries and errors

@pytest.mark.parametrize("status", [403, 429, 503, 408, 500])
def test_retry_statuses_succeed_on_the_next_try(fo, no_sleep, status):
    fo.script = [(status, {"error": {"message": "try again"}})]
    data, _, _ = client(fo).generate("x", [])
    assert len(fo.requests) == 2 and Image.open(io.BytesIO(data)).size == (320, 192)
    assert len(no_sleep) == 1


def test_backoff_grows_and_retry_after_is_honoured(fo, no_sleep):
    fo.script = [(503, "x"), (503, "x"), (503, "x")]
    client(fo, backoff=1.0).generate("x", [])
    assert no_sleep == [1.0, 2.0, 4.0]
    no_sleep.clear()
    fo.retry_after = 9
    fo.script = [(429, "slow down")]
    client(fo, backoff=1.0).generate("x", [])
    assert no_sleep == [9.0]


def test_retries_are_used_up_then_the_last_answer_is_reported(fo, no_sleep):
    fo.script = [(503, "overloaded")] * 10
    with pytest.raises(RouterError, match=r"after 3 tries.*HTTP 503: overloaded"):
        client(fo, retries=2).generate("x", [])
    assert len(fo.requests) == 3


def test_a_400_raises_with_the_body_text_and_is_not_retried(fo):
    fo.script = [(400, {"error": {"message": "resolution 9K is not supported"}})]
    with pytest.raises(RouterError, match=r"HTTP 400.*resolution 9K is not supported"):
        client(fo).generate("x", [])
    assert len(fo.requests) == 1


def test_a_long_error_body_is_trimmed_and_the_key_is_scrubbed(fo):
    fo.script = [(400, f"bad {KEY} " + "x" * 5000)]
    with pytest.raises(RouterError) as e:
        client(fo).generate("x", [])
    assert KEY not in str(e.value) and "***" in str(e.value) and len(str(e.value)) < 700


def test_401_is_not_retried(fo):
    with pytest.raises(RouterError, match="HTTP 401"):
        OpenRouter("wrong-key-000000", fo.url).generate("x", [])
    assert len(fo.requests) == 1


def test_connection_errors_are_retried_then_reported(no_sleep):
    with FakeOpenRouter() as f:
        url = f.url
    c = OpenRouter(KEY, url, retries=1, timeout=5)              # the server is gone
    with pytest.raises(RouterError, match=r"did not answer after 2 tries"):
        c.generate("x", [])
    assert len(no_sleep) == 1


def test_the_repr_never_shows_the_key(fo):
    assert KEY not in repr(client(fo))


# ---------------------------------------------------------------- the key

@pytest.fixture
def places(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    earlier = tmp_path / "earlier.env"
    monkeypatch.setattr(korouter, "EARLIER_TEST_ENV", earlier)
    return home / "secrets.env", earlier


def test_key_lookup_order_is_environment_then_secrets_then_the_earlier_env_file(places, monkeypatch):
    secrets, earlier = places
    earlier.write_text('# comment\nOTHER=1\nexport OPENROUTER_API_KEY="from-earlier"\n', encoding="utf-8")
    assert korouter.find_key() == "from-earlier"
    secrets.write_text("GEMINI_API_KEY=g\nOPENROUTER_API_KEY=from-secrets\n", encoding="utf-8")
    assert korouter.find_key() == "from-secrets"
    monkeypatch.setenv("OPENROUTER_API_KEY", "  from-env  ")
    assert korouter.find_key() == "from-env"


def test_a_blank_key_in_the_environment_falls_through(places, monkeypatch):
    secrets, _ = places
    secrets.write_text("OPENROUTER_API_KEY='quoted'\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
    assert korouter.find_key() == "quoted"


def test_no_key_names_where_it_looked(places):
    secrets, earlier = places
    with pytest.raises(RouterError) as e:
        korouter.find_key()
    msg = str(e.value)
    assert "OPENROUTER_API_KEY" in msg and str(secrets) in msg and str(earlier) in msg


# ---------------------------------------------------------------- the command options

def test_run_options_default_to_kaggle_and_accept_openrouter_settings():
    a = ktrial_cmd.build_parser("run").parse_args(["--chapter", "24"])
    assert (a.via, a.resolution, a.budget) == ("kaggle", "1K", 1.5)
    a = ktrial_cmd.build_parser("run").parse_args(["--chapter", "24", "--via", "openrouter", "--resolution", "2K",
                                                   "--budget", "0.25", "--only", "s01"])
    assert (a.via, a.resolution, a.budget, a.only) == ("openrouter", "2K", 0.25, "s01")
    for bad in (["--via", "fal"], ["--resolution", "4K"], ["--budget", "lots"]):
        with pytest.raises(SystemExit):
            ktrial_cmd.build_parser("run").parse_args(["--chapter", "24"] + bad)
    assert "--via kaggle|openrouter" in ktrial_cmd.USAGE and "--budget D" in ktrial_cmd.USAGE
    assert not hasattr(ktrial_cmd.build_parser("report").parse_args(["--chapter", "1"]), "via")


# ---------------------------------------------------------------- MiniMax H3 Image through OpenRouter

MINIMAX = "minimax/minimax-h3-image"


def test_minimax_uses_picture_wording(monkeypatch):
    monkeypatch.setattr(korouter, "MODEL", MINIMAX)
    assert korouter.hosted_text("<ref_image_0> and <ref_image_1>") == "<Picture 1> and <Picture 2>"
    assert korouter.hosted_text("no references") == "no references"


def test_minimax_request_has_no_seed_and_switches_prompt_enhancement_off(fo, monkeypatch):
    monkeypatch.setattr(korouter, "MODEL", MINIMAX)
    client(fo).generate("plain place", [], resolution="1K", seed=77)
    body = fo.requests[0]["body"]
    assert body["model"] == MINIMAX and body["resolution"] == "1K"
    assert "seed" not in body
    assert body["provider"] == {"options": {"minimax": {"disable_prompt_enhanced": True}}}


def test_minimax_edits_do_not_send_the_enhancement_switch(fo, monkeypatch):
    # the endpoint answers HTTP 400 to it when references are attached ("prompts are always enhanced")
    monkeypatch.setattr(korouter, "MODEL", MINIMAX)
    client(fo).generate("A <Picture 1> B", [ref_bytes((9, 9, 9))], resolution="1K", seed=77)
    body = fo.requests[0]["body"]
    assert "provider" not in body and "seed" not in body and len(body["input_references"]) == 1


def test_other_models_are_unchanged_by_the_minimax_branch(fo):
    client(fo).generate("x", [], seed=5)
    body = fo.requests[0]["body"]
    assert body["seed"] == 5 and "provider" not in body
