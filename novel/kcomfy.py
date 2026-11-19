"""A small ComfyUI client for the FLUX.3 [klein] trial, plus the workflow graph it submits.

Standard library only: the Kaggle notebook (kaggle/comfy_server.ipynb) copies this file next to itself and
imports it, so the probe there runs exactly the graph the local `novel ktrial run` sends.

Sources for every model-specific constant below (read 2026-11-16):
  - https://huggingface.co/black-forest-labs/flux-3-image-klein-comfy (model card, file list via the HF tree API)
  - https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/image_flux3_klein_distilled_image_edit.json
  - https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/image_flux3_klein_distilled_t2i.json
  - https://docs.comfy.org/tutorials/flux/flux-3-klein
  - ComfyUI v0.45.0 source: comfy_extras/nodes_edit_model.py (ReferenceMultiLatent), comfy_extras/nodes_flux.py
    (EmptyFlux2LatentImage), nodes.py (CLIPLoader type "flux3"), comfy_api/latest/_io.py (dotted names of
    DynamicCombo / Autogrow inputs in the API format), server.py (/prompt, /upload/image, /history, /view).

The prompt upsampler (qwen3.5_9b_flux3_prompt_upsampler_*) is deliberately not part of this graph: it is 6.7-18.8 GB
and rewrites the prompt. Our prompts are written as full captions, which is the case the model card allows it off.

A second model, MiniMax-H3-Image (GGUF), is a choice next to FLUX.3 klein: `build_workflow(..., model=MINIMAX)`.
Sources for it (read 2026-11-16; nothing below was taken from memory):
  - ComfyUI v0.45.0 (commit 989d93d, tag of 2026-11-10) contains PR #17655 "Support MiniMax-H3-image" (merge commit
    ede4e5f is an ancestor of the tag): comfy_extras/nodes_minimax_h3.py (MiniMaxH3ImageConditioning: inputs clip, vae,
    prompt, width, height, images.image_1..; outputs positive and latent; "<Picture n>" in the prompt; size step 32;
    references are rescaled to a 1024..2048 short edge by the node), comfy/supported_models.py (MiniMaxH3Image),
    comfy/ldm/minimax/sampling.py (the 6-step sigma grid comes from the model file's `pdd_sigmas`, no sampling node),
    comfy/text_encoders/minimax.py, comfy/sd.py (CLIPType.MINIMAX, TEModel.QWEN3VL_32B), nodes.py (CLIPLoader type
    "minimax"), comfy/model_management.py (unet_dtype / unet_manual_cast: on a T4 the default is emulated bf16).
  - https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/image_minimax_h3_image_t2i.json and
    image_minimax_h3_image_edit.json: UNETLoader -> KSampler(6 steps, cfg 1, euler, simple, denoise 1), the negative is
    ConditioningZeroOut of the positive, VAELoader minimax_h3_image_vae_fp16.safetensors, CLIPLoader type "minimax".
  - https://huggingface.co/AtomicChat/MiniMax-H3-Image-GGUF (card: files load in stock ComfyUI-GGUF commit 6ea2651 with
    "Unet Loader (GGUF)"; header read: architecture "wan", `config` with the 6 pdd_sigmas) and
    https://huggingface.co/Abiray/Qwen3-VL-32B-Instruct-GGUF (Qwen3vl_32b_h3-Q3_K_M.gguf: header read: architecture
    "qwen3vl", 50 layers, ComfyUI key names, vision tower included, comfy.gguf.orig_shape keys).
  - https://github.com/city96/ComfyUI-GGUF at 6ea2651e (loader.py, nodes.py: UnetLoaderGGUF, CLIPLoaderGGUF).
  - https://huggingface.co/MiniMaxAI/MiniMax-H3-Image (card: 6 steps, no CFG, no negative prompt, working range about
    4-20 MP, `<Picture n>` follows upload order). The picture size here (1.06 MP) is BELOW that range.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- versions and files

COMFY_TAG = "v0.45.0"           # ComfyUI release tag (2026-11-10). FLUX.3 support and ReferenceMultiLatent landed in
                                # v0.41.0; docs.comfy.org says "ComfyUI 0.41.0 or later".
HF_REPO = "black-forest-labs/flux-3-image-klein-comfy"   # gated: accept the FLUX Kommunity License first

# (path in the HF repo == path under ComfyUI/models, size in bytes from the HF tree API)
MODEL_FILES = [
    ("diffusion_models/flux3-image-klein-int8-convrot.safetensors", 5827874779),   # distilled, INT8 ConvRot
    ("text_encoders/qwen3vl_4b_int8_convrot.safetensors", 4858559240),            # Qwen3-VL 4B text encoder
    ("vae/flux2-vae.safetensors", 336211292),
]

UNET_NAME = "flux3-image-klein-int8-convrot.safetensors"
CLIP_NAME = "qwen3vl_4b_int8_convrot.safetensors"
CLIP_TYPE = "flux3"
VAE_NAME = "flux2-vae.safetensors"

# ---------------------------------------------------------------- sampling (the distilled templates' KSampler)

STEPS = 16                      # model card: "Klein (distilled): 16 steps, no guidance". 10 was tried for speed
                                # (33 s, not 54 s on a T4) and the chapter came out with too many bad hands.
CFG = 1.0
SAMPLER = "euler"
SCHEDULER = "simple"
DENOISE = 1.0
REF_MEGAPIXELS = 0.25           # template uses 1.0; on a T4 two references then took 135 s a picture. At
                                # 0.25 the face, clothes and place held just as well in 54 s (16 steps).
MAX_REFS = 7                    # our limit (was 3 = place + 2 people); the model takes up to 10
SIZE_STEP = 16                  # EmptyFlux2LatentImage width/height step


def ref_token(i: int) -> str:
    """How the prompt names the i-th reference (0-based): `<ref_image_0>`, `<ref_image_1>`, ... in the order the
    images are connected to ReferenceMultiLatent (model card, template note, docs.comfy.org)."""
    return f"<ref_image_{i}>"


# UNVERIFIED: whether the INT8 ConvRot build runs well on a Turing T4. ComfyUI v0.45.0 lists only bf16/fp32 as
# FLUX.3 inference dtypes (supported_models.Flux3) and the T4 has no native bf16; int8 matmul goes through
# torch._int_mm. The notebook's probe answers it. If the probe is too slow, try ["--fp16-unet"] (fp16 is NOT a listed
# FLUX.3 dtype: watch for black pictures, which the probe also catches). The notebook takes session["comfy_args"]
# over this default, so a retry needs no code change.
COMFY_LAUNCH_ARGS: list[str] = []

STOP_NAME = "KTRIAL_STOP.png"

# ---------------------------------------------------------------- the second model: MiniMax-H3-Image (GGUF)

FLUX = "flux"
MINIMAX = "minimax-h3"
QWEN = "qwen-image-turbo"
MODELS = (FLUX, MINIMAX, QWEN)

MM_DIR = "minimax-h3"           # the trial subfolder this model's pictures go to (images/, plates/, log.jsonl)

# (repo, path in the repo, folder under ComfyUI/models, bytes from the HF tree API 2026-11-16). None is gated.
MM_FILES = [
    ("AtomicChat/MiniMax-H3-Image-GGUF", "MiniMax-H3-Image-AD-Q4_K.gguf", "diffusion_models", 11571097824),
    ("Abiray/Qwen3-VL-32B-Instruct-GGUF", "Qwen3vl_32b_h3-Q3_K_M.gguf", "text_encoders", 12001428448),
    ("Comfy-Org/MiniMax-H3-Image", "vae/minimax_h3_image_vae_fp16.safetensors", "vae", 1168505312),
]
MM_UNET_NAME = "MiniMax-H3-Image-AD-Q4_K.gguf"
MM_CLIP_NAME = "Qwen3vl_32b_h3-Q3_K_M.gguf"
MM_CLIP_TYPE = "minimax"
MM_VAE_NAME = "minimax_h3_image_vae_fp16.safetensors"
MM_DOWNLOAD_GB = 26             # what the three files need on /tmp (checked before the download starts)

# Custom nodes (name, git url, full commit). ComfyUI-GGUF: the commit the AtomicChat card was tested with; it is also
# the head of the repository on 2026-11-16.
MM_NODES = [("ComfyUI-GGUF", "https://github.com/city96/ComfyUI-GGUF", "6ea2651e7df66d7585f6ffee804b20e92fb38b8a")]

MM_STEPS = 6                    # template KSampler; the model card: other values are rejected
MM_CFG = 1.0
MM_SAMPLER = "euler"
MM_SCHEDULER = "simple"
MM_SIZE_STEP = 32               # MiniMaxH3ImageConditioning width/height step (one DiT token = 32x32 pixels)
MM_TIMEOUT = 1500.0             # seconds to wait for one picture (the first one also loads 24 GB of weights)
MM_PROBE_TIMEOUT = 900.0        # the same for a notebook probe picture: a layout slower than this is not usable

# Launch layouts the notebook tries in this order (the first whose probe pictures are not blank wins). "visible" is
# CUDA_VISIBLE_DEVICES ("" = both cards). The diffusion model is loaded by KTrialUnetLoaderGGUF on the LAST visible
# card; the text encoder (and the VAE) stay on ComfyUI's default card, the first one. With one visible card both are on
# it and ComfyUI swaps them. --disable-dynamic-vram: classic estimate-based loading, which ComfyUI-GGUF's patcher was
# written for. --fp16-unet: a T4 has no native bf16 and ComfyUI's default there is emulated bf16 or fp32 for this model
# (comfy/model_management.py); fp16 is NOT a listed dtype of MiniMaxH3Image, so a black picture is possible and the
# probe catches it.
#
# Start 3 on Kaggle (2026-11-16) settled the dtype: every --fp16-unet layout made a black picture ("nodes.py:1708:
# RuntimeWarning: invalid value encountered in cast", i.e. NaN), at 4.3 s/step on one T4. --fp32-unet made a real text
# picture (226 s for the first one, loads included), but its reference probe ran out of memory on card 0 inside
# MiniMaxH3ImageConditioning (13.96 GiB allocated: the 12.6 GB text encoder fully loaded plus the VAE plus the vision
# tower). --reserve-vram is per card in ComfyUI (model_management.EXTRA_RESERVED_VRAM): with 2 GB the text encoder is
# loaded partly (11.9 of 12.6 GB in start 3) and the diffusion model (11.3 GB) still fits whole on card 1.
MM_LAYOUTS = [
    {"name": "two-cards-fp32-reserve2", "visible": "",
     "args": ["--disable-dynamic-vram", "--fp32-unet", "--reserve-vram", "2"]},
    {"name": "two-cards-fp32-reserve3", "visible": "",
     "args": ["--disable-dynamic-vram", "--fp32-unet", "--reserve-vram", "3"]},
    # The VAE on the diffusion model's card (card 1), so card 0 holds only the text encoder and its vision tower. The
    # graph is the same as for the other layouts: the notebook sets KTRIAL_VAE_DEVICE for this ComfyUI process only and
    # ktrial_nodes.py then makes the stock VAELoader build the VAE on the last card (KTrialVAELoader). Idea from a
    # third-party MiniMax H3 video notebook for T4 x2 (its VAE loader builds comfy.sd.VAE(sd, device=..., metadata=...)),
    # checked against ComfyUI v0.45.0: comfy/sd.py VAE.__init__ takes `device` and uses it as the patcher's load_device;
    # the VAE dtype comes from model_management.vae_dtype (fp16 on a T4 in start 3, also under --fp32-unet).
    # No PYTORCH_CUDA_ALLOC_CONF=expandable_segments: ComfyUI 0.45 with a cu130 torch turns cudaMallocAsync on and
    # appends ",backend:cudaMallocAsync" to that variable (cuda_malloc.py); expandable_segments is a setting of the native
    # caching allocator, which that backend replaces.
    {"name": "two-cards-fp32-vae-card1", "visible": "", "args": ["--disable-dynamic-vram", "--fp32-unet"],
     "env": {"KTRIAL_VAE_DEVICE": "last_gpu"}},
]

# Start 2 on Kaggle: the text encoder then loaded (12.6 GB on a card with 13.6 GB usable) and encoded, but the wrapper
# below hid the `metadata` parameter of load_diffusion_model_state_dict from ComfyUI-GGUF (it tests the signature), so
# the GGUF's `config` (pdd_sigmas) never arrived: "MiniMaxH3ImageModel.__init__() missing ... 'pdd_sigmas'". Fixed with
# functools.wraps.
#
# Start 1 on Kaggle (2026-11-16): the text encoder alone ran out of memory on a 14.56 GiB T4 ("Allocation on device 0
# ... Requested 741.88 MiB ... dequantize_blocks_Q3_K" with 14.30 GiB allocated, in MiniMaxH3ImageConditioning). 741.88 MiB
# is 151936 x 5120 bytes: the Q3_K token-embedding table, dequantized in full on the card at every forward. ComfyUI-GGUF
# (loader.gguf_clip_loader) dequantizes a token table to fp16 on the CPU only under the llama.cpp key name
# `token_embd.weight`; this encoder file uses the ComfyUI name `model.embed_tokens.weight`, so that step is skipped.
# KTrialCLIPLoaderGGUF does it for that name.
#
# Written by the notebook to ComfyUI/custom_nodes/ktrial_nodes.py. Core ComfyUI has no per-node device choice for a
# GGUF model (ComfyUI-MultiGPU's CLIPLoaderGGUFMultiGPU passes device='default' to CLIPLoaderGGUF.load_clip, which has no
# such parameter, and the project is archived), so this wraps UnetLoaderGGUF and hands comfy.sd.load_diffusion_model_
# state_dict the documented model_options["load_device"] (comfy/sd.py reads it there).
KTRIAL_NODES_PY = """import functools
import logging
import os
import sys

import torch

import comfy.sd
import comfy.utils
import folder_paths
import nodes

DEVICES = ["last_gpu", "default"]
EMBED = "model.embed_tokens.weight"


class KTrialUnetLoaderGGUF:
    \"\"\"UnetLoaderGGUF, with the model on the last visible CUDA card (the default device when only one is visible).\"\"\"

    @classmethod
    def INPUT_TYPES(s):
        base = nodes.NODE_CLASS_MAPPINGS["UnetLoaderGGUF"].INPUT_TYPES()
        return {"required": {"unet_name": base["required"]["unet_name"], "device": (DEVICES,)}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load_unet"
    CATEGORY = "ktrial"

    def load_unet(self, unet_name, device):
        target = None
        if device == "last_gpu" and torch.cuda.is_available() and torch.cuda.device_count() > 1:
            target = torch.device("cuda", torch.cuda.device_count() - 1)
        real = comfy.sd.load_diffusion_model_state_dict

        @functools.wraps(real)          # ComfyUI-GGUF reads the signature to decide whether to pass `metadata`
        def patched(sd, model_options=None, **kw):
            mo = dict(model_options or {})
            if target is not None:
                mo["load_device"] = target
            return real(sd, model_options=mo, **kw)

        comfy.sd.load_diffusion_model_state_dict = patched
        try:
            out = nodes.NODE_CLASS_MAPPINGS["UnetLoaderGGUF"]().load_unet(unet_name)
        finally:
            comfy.sd.load_diffusion_model_state_dict = real
        logging.info("KTrialUnetLoaderGGUF: %s on %s", unet_name, target or "the default device")
        return out


class KTrialCLIPLoaderGGUF:
    \"\"\"CLIPLoaderGGUF that dequantizes the token-embedding table to fp16 on the CPU, as ComfyUI-GGUF does for its own
    key name (see the note above).\"\"\"

    @classmethod
    def INPUT_TYPES(s):
        return nodes.NODE_CLASS_MAPPINGS["CLIPLoaderGGUF"].INPUT_TYPES()

    RETURN_TYPES = ("CLIP",)
    FUNCTION = "load_clip"
    CATEGORY = "ktrial"

    def load_clip(self, clip_name, type="stable_diffusion"):
        base_cls = nodes.NODE_CLASS_MAPPINGS["CLIPLoaderGGUF"]
        loader = sys.modules[sys.modules[base_cls.__module__].gguf_clip_loader.__module__]

        class Patched(base_cls):
            def load_data(self, ckpt_paths):
                data = super().load_data(ckpt_paths)
                for sd in data:
                    w = sd.get(EMBED)
                    if w is not None and loader.is_quantized(w):
                        sd[EMBED] = loader.dequantize_tensor(w, dtype=torch.float16)
                        logging.info("KTrialCLIPLoaderGGUF: %s dequantized to fp16 on the CPU", EMBED)
                return data

        return Patched().load_clip(clip_name, type)


def _last_gpu():
    if torch.cuda.is_available() and torch.cuda.device_count() > 1:
        return torch.device("cuda", torch.cuda.device_count() - 1)
    return None


class KTrialVAELoader:
    \"\"\"A VAE file from models/vae built on the last visible CUDA card (comfy.sd.VAE's `device`), the way the stock
    VAELoader builds it otherwise (load_torch_file with metadata, then throw_exception_if_invalid).\"\"\"

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"vae_name": (folder_paths.get_filename_list("vae"),), "device": (DEVICES,)}}

    RETURN_TYPES = ("VAE",)
    FUNCTION = "load_vae"
    CATEGORY = "ktrial"

    def load_vae(self, vae_name, device="last_gpu"):
        target = _last_gpu() if device == "last_gpu" else None
        sd, metadata = comfy.utils.load_torch_file(folder_paths.get_full_path_or_raise("vae", vae_name),
                                                   return_metadata=True)
        vae = comfy.sd.VAE(sd=sd, device=target, metadata=metadata)
        vae.throw_exception_if_invalid()
        logging.info("KTrialVAELoader: %s on %s", vae_name, target or "the default device")
        return (vae,)


# A launch layout can move the VAE of an unchanged graph: with KTRIAL_VAE_DEVICE=last_gpu in this process's environment
# the stock VAELoader hands a plain VAE file to KTrialVAELoader (special names such as pixel_space or a TAE are left to
# it). Unset, nothing is patched.
if os.environ.get("KTRIAL_VAE_DEVICE") == "last_gpu":
    _stock_load_vae = nodes.VAELoader.load_vae

    def _load_vae(self, vae_name):
        if vae_name in folder_paths.get_filename_list("vae"):
            return KTrialVAELoader().load_vae(vae_name, "last_gpu")
        return _stock_load_vae(self, vae_name)

    nodes.VAELoader.load_vae = _load_vae
    logging.info("ktrial_nodes: VAELoader builds VAEs on the last card (KTRIAL_VAE_DEVICE)")


# The same for the text encoder of an unchanged graph (Qwen-Image on two T4s): with KTRIAL_CLIP_DEVICE=last_gpu the stock
# CLIPLoader hands comfy.sd.load_clip the documented model_options["load_device"] (the CPU choice of the stock node uses
# the same key), so the encoder lives on the last card and never has to leave the diffusion model's card.
if os.environ.get("KTRIAL_CLIP_DEVICE") == "last_gpu":
    _stock_load_clip = nodes.CLIPLoader.load_clip

    def _load_clip(self, clip_name, type="stable_diffusion", device="default"):
        target = _last_gpu()
        if target is None or device != "default":
            return _stock_load_clip(self, clip_name, type, device)
        real = comfy.sd.load_clip

        @functools.wraps(real)
        def patched(*args, **kw):
            mo = dict(kw.pop("model_options", None) or {})
            mo.setdefault("load_device", target)
            return real(*args, model_options=mo, **kw)

        comfy.sd.load_clip = patched
        try:
            out = _stock_load_clip(self, clip_name, type, device)
        finally:
            comfy.sd.load_clip = real
        logging.info("ktrial_nodes: CLIPLoader put %s on %s (KTRIAL_CLIP_DEVICE)", clip_name, target)
        return out

    nodes.CLIPLoader.load_clip = _load_clip
    logging.info("ktrial_nodes: CLIPLoader builds text encoders on the last card (KTRIAL_CLIP_DEVICE)")


NODE_CLASS_MAPPINGS = {"KTrialUnetLoaderGGUF": KTrialUnetLoaderGGUF, "KTrialCLIPLoaderGGUF": KTrialCLIPLoaderGGUF,
                       "KTrialVAELoader": KTrialVAELoader}
"""


# ---------------------------------------------------------------- the third model: Qwen-Image-2.1-Turbo (INT8)
#
# Sources (read 2026-11-17; the graph is the official Turbo templates, not memory):
#   - https://huggingface.co/Comfy-Org/Qwen-Image-2.1 (file list and byte sizes from its HF tree API; not gated) and
#     https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo (license: qwen-research).
#   - https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/image_qwen_image_2_1_turbo_t2i.json
#     and image_qwen_image_2_1_turbo_image_edit.json: UNETLoader (turbo int8 convrot) + CLIPLoader type "qwen_image"
#     (qwen3vl_8b_int8_convrot) + VAELoader -> TextEncodeQwenImage21 -> CFGGuider(cfg 1) -> SamplerCustomAdvanced with
#     RandomNoise, KSamplerSelect euler and ManualSigmas (the fixed 8-step schedule below), EmptyLatentImage for the size.
#     The template notes: "image_1 is the edit target, the rest are references", "output follows image_1" unless the
#     latent is sized by hand, "mention them in the prompt as <image1>, <image2>", "cfg: keep 1".
#   - ComfyUI v0.45.0 (our pin) already has comfy_extras/nodes_qwen.py: TextEncodeQwenImage21 (inputs clip, prompt,
#     negative_prompt, vae, resolution, images.image_1..16; outputs positive, negative, latent) and
#     supported_models.QwenImage21 (supported_inference_dtypes bf16 and fp32 only, like FLUX.3: no custom node needed).
# The optional prompt enhancer (qwen3.5_9b_*_pe_*) and the experimental QwenImage21Cache are left out.

QW_DIR = "qwen-image-turbo"     # the trial subfolder this model's pictures go to (images/, plates/, log.jsonl)

QW_REPO = "Comfy-Org/Qwen-Image-2.1"
# (repo, path in the repo == path under ComfyUI/models, folder, bytes from the HF tree API 2026-11-17)
QW_FILES = [
    (QW_REPO, "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors", "diffusion_models", 7256787829),
    (QW_REPO, "text_encoders/qwen3vl_8b_int8_convrot.safetensors", "text_encoders", 9350798360),
    (QW_REPO, "vae/qwen_image_2.1_vae_bf16.safetensors", "vae", 675509688),
]
QW_UNET_NAME = "qwen_image_2.1_turbo_int8_convrot.safetensors"
QW_CLIP_NAME = "qwen3vl_8b_int8_convrot.safetensors"
QW_CLIP_TYPE = "qwen_image"
QW_VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
QW_DOWNLOAD_GB = 18             # the three files need 17.3 GB on /tmp (checked before the download starts)

QW_STEPS = 8                    # the Turbo checkpoint's fixed schedule: 8 steps, ending at sigma 0
QW_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"
QW_CFG = 1.0                    # templates: "keep 1"; the negative prompt is unused at cfg 1
QW_SAMPLER = "euler"
QW_SIZE_STEP = 32               # one DiT token = 32x32 pixels (TextEncodeQwenImage21 rounds references to 32)
# TextEncodeQwenImage21 `resolution`: every reference is resized to about resolution x resolution pixels (the template
# default is 1024). 768 is a guess between that and FLUX's 0.25 MP (two references at 1 MP took 135 s a picture on a T4
# there): tune it from the first run's seconds.
QW_REF_RESOLUTION = 768
QW_TIMEOUT = 1500.0             # seconds to wait for one picture (the first one also loads 16.6 GB of weights)
QW_PROBE_TIMEOUT = 900.0

# Launch layouts the notebook tries in this order (the first whose probe pictures are not blank wins), all on the one
# card `session["gpu"]` names (no custom loader here, so ComfyUI uses one card whatever is visible). The default matches
# FLUX.3, whose INT8 build runs on the T4 with no arguments; --fp32-unet is what cured MiniMax's black pictures (the
# model lists only bf16/fp32 and a T4 has no native bf16).
QW_LAYOUTS = [
    {"name": "one-card-default", "args": []},
    {"name": "one-card-fp32-unet", "args": ["--fp32-unet"]},
]
# Both T4s of the notebook: the diffusion model and the VAE on card 0 (ComfyUI's default), the 9.3 GB text encoder on
# card 1 (ktrial_nodes.py, KTRIAL_CLIP_DEVICE). On one 15 GB card the 7.2 GB model and the encoder do not fit together, so
# ComfyUI moved the encoder in and out for every picture. Tried first when the session asks for it (split_encoder);
# the one-card layouts stay behind it as the fallback.
QW_SPLIT_LAYOUT = {"name": "two-cards-encoder-card1", "visible": "", "args": [],
                   "env": {"KTRIAL_CLIP_DEVICE": "last_gpu"}}

_SIZE_STEPS = {FLUX: SIZE_STEP, MINIMAX: MM_SIZE_STEP, QWEN: QW_SIZE_STEP}


def adapt_prompt(model: str, text: str) -> str:
    """The prompt as the model wants it. FLUX: unchanged (`<ref_image_0>`). MiniMax-H3-Image: `<Picture 1>`, 1-based, in
    the order the images are connected (model card; MiniMaxH3ImageConditioning's description). Qwen-Image 2.1:
    `<image1>`, 1-based, in the order the images are connected (the Turbo template notes)."""
    if model == MINIMAX:
        return re.sub(r"<ref_image_(\d+)>", lambda m: f"<Picture {int(m.group(1)) + 1}>", text)
    if model == QWEN:
        return re.sub(r"<ref_image_(\d+)>", lambda m: f"<image{int(m.group(1)) + 1}>", text)
    return text


def qwen_order(text: str, refs: list[dict]) -> tuple[str, list[dict]]:
    """Qwen-Image 2.1 edits `image_1` (its size and place are kept, the others are references), and our canvas is the
    place plate: put it first and renumber the `<ref_image_N>` of the prompt to match. Without a place the order and the
    text stay as they were."""
    order = sorted(range(len(refs)), key=lambda i: refs[i].get("role") != "place")     # stable: the place first
    new = {old: n for n, old in enumerate(order)}
    text = re.sub(r"<ref_image_(\d+)>", lambda m: f"<ref_image_{new.get(int(m.group(1)), int(m.group(1)))}>", text)
    return text, [refs[i] for i in order]


def model_info(model: str) -> dict:
    """What a picture's meta file records about the model."""
    if model == MINIMAX:
        return {"model": MM_UNET_NAME, "text_encoder": MM_CLIP_NAME, "steps": MM_STEPS}
    if model == QWEN:
        return {"model": QW_UNET_NAME, "text_encoder": QW_CLIP_NAME, "steps": QW_STEPS}
    return {"model": UNET_NAME, "steps": STEPS}


def download_plan(model: str) -> list[tuple[str, str, str, int]]:
    """(repo, path in the repo, folder under ComfyUI/models, bytes) for every file the model needs."""
    if model == MINIMAX:
        return list(MM_FILES)
    if model == QWEN:
        return list(QW_FILES)
    return [(HF_REPO, rel, str(Path(rel).parent), size) for rel, size in MODEL_FILES]


def download_gb(model: str) -> int:
    """The /tmp space (GB) the notebook demands for the model's files; 0 for FLUX (its files are small)."""
    return {MINIMAX: MM_DOWNLOAD_GB, QWEN: QW_DOWNLOAD_GB}.get(model, 0)


def picture_timeout(model: str) -> float:
    """Seconds `run` waits for one picture (the first one also loads the weights)."""
    return {MINIMAX: MM_TIMEOUT, QWEN: QW_TIMEOUT}.get(model, 600.0)


def probe_timeout(model: str) -> float:
    """The same for a notebook probe picture: a layout slower than this is not usable."""
    return {MINIMAX: MM_PROBE_TIMEOUT, QWEN: QW_PROBE_TIMEOUT}.get(model, 1800.0)


def layouts(model: str, comfy_args: list[str] | None = None, flux_visible: str = "",
            split_encoder: bool = False) -> list[dict]:
    """The launch layouts to try: FLUX has one (its `visible` card, the session's args), MiniMax and Qwen the lists
    above (Qwen on the `flux_visible` card; with `split_encoder` the two-card layout first). Given `comfy_args` replace
    each layout's arguments."""
    if model == MINIMAX:
        out = [dict(l, args=list(comfy_args) if comfy_args is not None else list(l["args"])) for l in MM_LAYOUTS]
        return out
    if model == QWEN:
        one = [dict(l, visible=flux_visible, args=list(comfy_args) if comfy_args is not None else list(l["args"]))
               for l in QW_LAYOUTS]
        return ([dict(QW_SPLIT_LAYOUT, args=list(comfy_args or []))] if split_encoder else []) + one
    return [{"name": "default", "visible": flux_visible,
             "args": list(comfy_args) if comfy_args is not None else list(COMFY_LAUNCH_ARGS)}]


class ComfyError(RuntimeError):
    """Anything that went wrong talking to ComfyUI, or a picture it could not make."""


# ---------------------------------------------------------------- the graph

def build_workflow(prompt: str, seed: int, width: int, height: int, refs: list[str], prefix: str,
                   model: str = FLUX) -> dict:
    """The API-format graph for one picture. `refs` are image names as returned by ComfyUI's upload (0..3).
    `model` FLUX (default) or MINIMAX (see _build_minimax).

    Text-to-image follows image_flux3_klein_distilled_t2i (empty negative); with references it follows
    image_flux3_klein_distilled_image_edit: the negative is the zeroed positive, every reference goes into both
    through ReferenceMultiLatent, and the size comes from EmptyFlux2LatentImage, as that template's note says
    ("Use `Empty Flux 2 Latent` node to customize image size")."""
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}: {', '.join(MODELS)}")
    refs = list(refs or [])
    if len(refs) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} reference images, got {len(refs)}")
    step = _SIZE_STEPS[model]
    if width % step or height % step or width <= 0 or height <= 0:
        raise ValueError(f"width and height must be positive multiples of {step}, got {width}x{height}")
    if not 0 <= int(seed) <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"seed out of range: {seed}")
    if model == MINIMAX:
        return _build_minimax(prompt, seed, width, height, refs, prefix)
    if model == QWEN:
        return _build_qwen(prompt, seed, width, height, refs, prefix)

    g: dict = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": CLIP_TYPE, "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
    }
    if refs:
        g["5"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}}
        multi = {"positive": ["4", 0], "negative": ["5", 0], "vae": ["3", 0],
                 "resolution": "resize", "resolution.megapixels": REF_MEGAPIXELS}
        for i, name in enumerate(refs):
            nid = str(20 + i)
            g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
            multi[f"images.image{i}"] = [nid, 0]
        g["10"] = {"class_type": "ReferenceMultiLatent", "inputs": multi}
        positive, negative = ["10", 0], ["10", 1]
    else:
        g["5"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}}
        positive, negative = ["4", 0], ["5", 0]

    g["7"] = {"class_type": "KSampler", "inputs": {
        "seed": int(seed), "steps": STEPS, "cfg": CFG, "sampler_name": SAMPLER, "scheduler": SCHEDULER,
        "denoise": DENOISE, "model": ["1", 0], "positive": positive, "negative": negative,
        "latent_image": ["6", 0]}}
    g["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    g["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}}
    return g


def _build_minimax(prompt: str, seed: int, width: int, height: int, refs: list[str], prefix: str) -> dict:
    """MiniMax-H3-Image, after the official image_minimax_h3_image_t2i / _edit templates with the prompt expander left
    out and the two loaders swapped for the GGUF ones: MiniMaxH3ImageConditioning (clip, vae, prompt, size, the
    references as images.image_1, images.image_2, ...) gives the positive conditioning and the latent; the negative is
    its ConditioningZeroOut; KSampler 6 steps, cfg 1, euler, simple. With references the same node is used (the
    template's edit graph), the first reference being `<Picture 1>`. The prompt is sent as it is: write `<Picture n>`
    with adapt_prompt()."""
    cond = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "width": width, "height": height}
    g: dict = {
        "1": {"class_type": "KTrialUnetLoaderGGUF", "inputs": {"unet_name": MM_UNET_NAME, "device": "last_gpu"}},
        "2": {"class_type": "KTrialCLIPLoaderGGUF", "inputs": {"clip_name": MM_CLIP_NAME, "type": MM_CLIP_TYPE}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MM_VAE_NAME}},
    }
    for i, name in enumerate(refs):
        nid = str(20 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        cond[f"images.image_{i + 1}"] = [nid, 0]
    g["4"] = {"class_type": "MiniMaxH3ImageConditioning", "inputs": cond}
    g["5"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}}
    g["7"] = {"class_type": "KSampler", "inputs": {
        "seed": int(seed), "steps": MM_STEPS, "cfg": MM_CFG, "sampler_name": MM_SAMPLER, "scheduler": MM_SCHEDULER,
        "denoise": 1.0, "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["4", 1]}}
    g["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    g["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}}
    return g


def _build_qwen(prompt: str, seed: int, width: int, height: int, refs: list[str], prefix: str) -> dict:
    """Qwen-Image-2.1-Turbo after the official Turbo templates (see the sources above): TextEncodeQwenImage21 gives the
    positive and negative conditioning (the references as images.image_1, images.image_2, ..., with the VAE so they
    also go in as latents); the canvas is our own EmptyLatentImage, as the template notes allow, because image_1 may
    not have our picture's shape. CFGGuider cfg 1, SamplerCustomAdvanced with euler and the fixed 8-step sigmas. The
    prompt is sent as it is: write `<image1>` with adapt_prompt() and put the canvas (the place) first with
    qwen_order()."""
    enc = {"clip": ["2", 0], "prompt": prompt, "negative_prompt": "", "resolution": QW_REF_RESOLUTION}
    g: dict = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": QW_UNET_NAME, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QW_CLIP_NAME, "type": QW_CLIP_TYPE, "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QW_VAE_NAME}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "10": {"class_type": "ManualSigmas", "inputs": {"sigmas": QW_SIGMAS}},
        "11": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": QW_SAMPLER}},
        "12": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
    }
    if refs:
        enc["vae"] = ["3", 0]
    for i, name in enumerate(refs):
        nid = str(20 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"images.image_{i + 1}"] = [nid, 0]
    g["4"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
    g["13"] = {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                                                     "cfg": QW_CFG}}
    g["7"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["12", 0], "guider": ["13", 0], "sampler": ["11", 0], "sigmas": ["10", 0], "latent_image": ["6", 0]}}
    g["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    g["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}}
    return g


SAVE_NODE = "9"


# ---------------------------------------------------------------- the client

RETRY_STATUS = (502, 503, 504)


class Comfy:
    """ComfyUI over HTTP. Every request carries basic auth when a user is given, and is retried with backoff on
    connection errors and 502/503/504 (tunnels drop). A 401 is never retried."""

    def __init__(self, base_url: str, user: str | None = None, password: str | None = None,
                 timeout: float = 60.0, retries: int = 5, backoff: float = 1.0, poll: float = 2.0):
        self.base = base_url.rstrip("/")
        self.poll = poll                    # seconds between /history polls in generate()
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.headers = {
            "User-Agent": "kcomfy/1",
            "ngrok-skip-browser-warning": "1",     # ngrok free plan: skip the browser interstitial
        }
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self.headers["Authorization"] = "Basic " + token

    # -- transport

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                 timeout: float | None = None) -> bytes:
        url = self.base + path
        hdrs = dict(self.headers, **(headers or {}))
        last = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * 2 ** (attempt - 1))
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                text = _read_err(e)
                if e.code in RETRY_STATUS:
                    last = f"HTTP {e.code} from {method} {path}"
                    continue
                if e.code == 401:
                    raise ComfyError(f"401 Unauthorized from {method} {path}: wrong or missing user/password") from None
                raise _HTTPStatus(e.code, text, f"HTTP {e.code} from {method} {path}: {text[:2000]}") from None
            except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError,
                    http.client.HTTPException) as e:
                last = f"{type(e).__name__}: {getattr(e, 'reason', e)} on {method} {path}"
                continue
        raise ComfyError(f"gave up after {self.retries + 1} tries: {last}")

    def _json(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        hdrs = {"Content-Type": "application/json"} if body is not None else None
        raw = self._request(method, path, body, hdrs)
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise ComfyError(f"{method} {path} did not return JSON: {raw[:200]!r}") from None

    # -- API

    def ping(self) -> dict:
        """GET /system_stats. Raises ComfyError when the server does not answer."""
        return self._json("GET", "/system_stats")

    def upload_image(self, path, name: str | None = None) -> str:
        """POST /upload/image (overwrite=true) and return the name ComfyUI stored it under."""
        p = Path(path)
        return self.upload_bytes(p.read_bytes(), name or p.name)

    def upload_bytes(self, data: bytes, name: str) -> str:
        boundary = "----kcomfy" + uuid.uuid4().hex
        ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
        parts = []
        for field, value in (("overwrite", "true"), ("type", "input")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        raw = self._request("POST", "/upload/image", b"".join(parts),
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            out = json.loads(raw)
        except ValueError:
            raise ComfyError(f"upload of {name} did not return JSON: {raw[:200]!r}") from None
        sub = out.get("subfolder") or ""
        return f"{sub}/{out['name']}" if sub else out["name"]

    def queue(self, workflow: dict) -> str:
        """POST /prompt and return the prompt id. A rejected graph raises ComfyError carrying node_errors.

        The id is chosen here, so a retry after a dropped connection resubmits the same id."""
        pid = str(uuid.uuid4())
        try:
            out = self._json("POST", "/prompt", {"prompt": workflow, "client_id": "kcomfy", "prompt_id": pid})
        except _HTTPStatus as e:
            try:
                info = json.loads(e.body)
            except ValueError:
                raise ComfyError(str(e)) from None
            raise ComfyError("ComfyUI rejected the graph: " + _describe_rejection(info)) from None
        if out.get("node_errors"):
            raise ComfyError("ComfyUI rejected the graph: " + _describe_rejection(out))
        return out.get("prompt_id") or pid

    def wait(self, prompt_id: str, timeout: float, poll: float = 2.0) -> dict:
        """GET /history/<id> until the prompt is done; returns its history entry. Raises on error or timeout."""
        end = time.monotonic() + timeout
        while True:
            hist = self._json("GET", f"/history/{prompt_id}") or {}
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(f"prompt {prompt_id} failed: {_describe_exec_error(status)}")
                if status.get("completed", True) and entry.get("outputs"):
                    return entry
                if status.get("completed"):
                    raise ComfyError(f"prompt {prompt_id} finished without outputs: {json.dumps(status)[:500]}")
            if time.monotonic() >= end:
                raise ComfyError(f"prompt {prompt_id} not done after {timeout:.0f}s")
            time.sleep(poll)

    def fetch(self, image_info: dict) -> bytes:
        """GET /view for one entry of a history entry's outputs[...]["images"]."""
        q = urllib.parse.urlencode({"filename": image_info["filename"],
                                    "subfolder": image_info.get("subfolder", ""),
                                    "type": image_info.get("type", "output")})
        return self._request("GET", "/view?" + q)

    def queue_size(self) -> int:
        """Running plus pending prompts (GET /queue)."""
        q = self._json("GET", "/queue") or {}
        return len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])

    def history_count(self) -> int:
        return len(self._json("GET", "/history") or {})

    def generate(self, prompt: str, seed: int, width: int, height: int, refs: list[str], prefix: str,
                 timeout: float = 600.0, model: str = FLUX) -> tuple[bytes, float]:
        """One picture: queue, wait, fetch. Returns the image bytes (what SaveImage wrote, PNG on a real
        ComfyUI) and the seconds from queueing to done."""
        wf = build_workflow(prompt, seed, width, height, refs, prefix, model)
        t0 = time.monotonic()
        pid = self.queue(wf)
        entry = self.wait(pid, timeout, self.poll)
        took = time.monotonic() - t0
        images = (entry.get("outputs", {}).get(SAVE_NODE) or {}).get("images") or []
        if not images:
            for out in entry.get("outputs", {}).values():
                images = out.get("images") or []
                if images:
                    break
        if not images:
            raise ComfyError(f"prompt {pid} produced no image")
        return self.fetch(images[0]), took


class _HTTPStatus(ComfyError):
    def __init__(self, code: int, body: str, msg: str):
        super().__init__(msg)
        self.code = code
        self.body = body


def _read_err(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _describe_rejection(info: dict) -> str:
    bits = []
    err = info.get("error")
    if isinstance(err, dict):
        bits.append(f"{err.get('type', '')}: {err.get('message', '')} {err.get('details', '')}".strip())
    elif err:
        bits.append(str(err))
    for nid, ne in (info.get("node_errors") or {}).items():
        for e in ne.get("errors") or []:
            bits.append(f"node {nid} ({ne.get('class_type', '?')}): {e.get('message', '')} {e.get('details', '')}".strip())
    return "; ".join(bits) or json.dumps(info)[:1000]


def _describe_exec_error(status: dict) -> str:
    for msg in status.get("messages") or []:
        if isinstance(msg, (list, tuple)) and len(msg) == 2 and msg[0] == "execution_error":
            d = msg[1] or {}
            return f"{d.get('node_type', '?')} (node {d.get('node_id', '?')}): " \
                   f"{d.get('exception_type', '')}: {d.get('exception_message', '')}".strip()
    return json.dumps(status)[:500]


# ---------------------------------------------------------------- stopping the server from the local side

def tiny_png() -> bytes:
    """A valid 1x1 grey PNG, standard library only."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)          # 1x1, 8-bit greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b"")


def request_stop(comfy: Comfy) -> str:
    """Upload KTRIAL_STOP.png; the notebook's keep-alive loop sees it in ComfyUI/input/ and shuts down."""
    return comfy.upload_bytes(tiny_png(), STOP_NAME)


# ---------------------------------------------------------------- basic-auth reverse proxy (runs on Kaggle)

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "authorization", "content-length"}


SECOND_PREFIX = "/g2"            # the second ComfyUI (the other T4), behind the same proxy and tunnel


def serve_auth_proxy(port: int, upstream: str, user: str, password: str, host: str = "127.0.0.1",
                     routes: dict | None = None):
    """Start a threaded reverse proxy on host:port that demands HTTP basic auth and forwards to `upstream`
    (e.g. http://127.0.0.1:8188). The tunnel (ngrok or cloudflared) points at this, so auth does not depend on
    the tunnel's plan. Returns the server; `server.last_request` is the time.time() of the last authorised
    request, `server.shutdown()` stops it. Runs in a daemon thread."""
    up = urllib.parse.urlsplit(upstream)
    # routes: {"/g2": "http://127.0.0.1:8190"}: a request under the prefix goes there, with the prefix taken off
    extra = {pre.rstrip("/"): urllib.parse.urlsplit(u) for pre, u in (routes or {}).items()}
    want = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _proxy(self):
            if self.headers.get("Authorization", "") != want:
                body = b"unauthorized"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="ktrial"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            server.last_request = time.time()
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
            # ComfyUI checks Origin against Host for browser requests; drop Origin, we are not a browser.
            hdrs.pop("Origin", None)
            hdrs.pop("origin", None)
            target, path = up, self.path
            for pre, u in extra.items():
                if path == pre or path.startswith(pre + "/") or path.startswith(pre + "?"):
                    target, path = u, path[len(pre):] or "/"
                    if path.startswith("?"):
                        path = "/" + path
                    break
            try:
                conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=600)
                conn.request(self.command, path, body=body, headers=hdrs)
                r = conn.getresponse()
                data = r.read()
                status, rh = r.status, r.getheaders()
                conn.close()
            except Exception as e:  # upstream down: tell the client to retry
                data = f"upstream error: {e}".encode()
                status, rh = 502, [("Content-Type", "text/plain")]
            self.send_response(status)
            for k, v in rh:
                if k.lower() not in _HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _proxy

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.last_request = time.time()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
