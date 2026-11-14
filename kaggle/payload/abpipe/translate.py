"""English to Hindi via sarvam-translate.

The one thing to understand here: sarvam-translate is a TRANSLATION model,
not an instruction-following chat model. Its model card permits exactly one
system string, "Translate the text below to {lang}." Anything else placed in
the prompt is not obeyed - it is translated and returned as part of the output.

This was learned the hard way. An earlier version of this file put a style
rule, a glossary table and rolling context into the system prompt; all of it
came back rendered into Hindi and prepended to every chapter, so the narrator
read the instructions aloud before starting the story.

Consequences for the design:

1. No rolling context in the prompt. The model is document-level and handles
   context within a block itself.

2. Terminology and register are applied AFTER generation, by apply_glossary()
   over CASUAL_SUBSTITUTIONS. Find-and-replace is normally the wrong tool for
   Hindi because words take case endings - but the substitutions here are
   English loanwords and invariant tadbhav nouns, which do not inflect.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


# Formal/tatsam Hindi -> everyday spoken Hindi. sarvam-translate cannot be
# steered on register (see build_messages), so this is applied to its output.
# Only invariant nouns: English loanwords and common tadbhav words, which do
# not take case endings, so plain string replacement is safe.
CASUAL_SUBSTITUTIONS = {
    # Multi-word entries first for readability, though apply_glossary sorts by
    # length anyway: "आदिम राक्षसी" must be replaced before bare "राक्षसी",
    # or the compound is destroyed before it can ever match.
    "आदिम राक्षसी": "प्रिमोर्डियल डेमोनेस",
    "आदिम": "प्रिमोर्डियल", "राक्षसी": "डेमोनेस",
    # Proper nouns: the model's own rendering -> the glossary's preferred one.
    # glossary.json is English->Hindi, which cannot be enforced after the fact
    # (the English is gone by then), so corrections have to be Hindi->Hindi.
    # These particular pairs were revealed by the leaked prompt rows, which
    # showed the model translating each glossary term its own way.
    "अनुक्रम": "सीक्वेंस", "बेयॉन्डर": "बियॉन्डर", "लुमियन": "लुमियान",
    "मार्ग": "पाथवे", "दैवीय": "डिवाइन", "देवदूत": "एंजल",
    "दर्पण": "मिरर", "प्रक्षेपण": "प्रोजेक्शन", "अद्वितीय": "यूनीक",
    "नियंत्रण": "कंट्रोल", "स्तर": "लेवल", "लक्ष्य": "टारगेट",
    "संपर्क": "कॉन्टैक्ट", "समस्या": "प्रॉब्लम", "योजना": "प्लान",
    "क्षेत्र": "एरिया", "सतह": "सरफेस", "ऊर्जा": "एनर्जी",
    "स्थिति": "हालात", "वास्तविकता": "हकीकत", "कारण": "वजह",
    "प्रभाव": "असर", "क्षमता": "काबिलियत", "सूचना": "जानकारी",
    "स्मृति": "याद", "स्वप्न": "सपना", "रहस्य": "राज़",
    "संकेत": "सिग्नल", "संदेश": "मैसेज", "प्रयोग": "एक्सपेरिमेंट",

    # ---- proper nouns to keep untranslated (added on request) ----
    # Bare मूर्ख (fool) and bare यात्रा (travel) are deliberately
    # ABSENT: the first is an adjective that inflects, the second an
    # everyday noun. Replacing either would corrupt ordinary prose, which
    # is exactly what apply_glossary()'s docstring warns against. Only the
    # title forms and non-inflecting compounds are listed.
    "स्वर्ग और पृथ्वी के आशीर्वाद के दिव्य योग्य": "सेलेस्टियल वर्दी ऑफ हेवन एंड अर्थ फॉर ब्लेसिंग्स",
    "दिव्य योग्य": "सेलेस्टियल वर्दी",
    "आकाशीय योग्य": "सेलेस्टियल वर्दी",
    "स्वर्गीय योग्य": "सेलेस्टियल वर्दी",
    "दिव्य गुरु": "सेलेस्टियल मास्टर",
    "आकाशीय गुरु": "सेलेस्टियल मास्टर",
    "स्वर्गीय गुरु": "सेलेस्टियल मास्टर",
    "दिव्य स्वामी": "सेलेस्टियल मास्टर",
    "सेफिराह महल": "सेफिराह कैसल",
    "सेफिरा महल": "सेफिराह कैसल",
    "सर्वशक्तिमान ईश्वर": "गॉड आल्माइटी",
    "सर्वशक्तिमान भगवान": "गॉड आल्माइटी",
    "सर्वशक्तिमान परमेश्वर": "गॉड आल्माइटी",
    "यात्री बैग": "ट्रैवलर बैग",
    "यात्रा बैग": "ट्रैवलर बैग",
    "यात्री का बैग": "ट्रैवलर बैग",
    "यात्री का थैला": "ट्रैवलर बैग",
    "यात्री": "ट्रैवलर",
    "श्री मूर्ख": "मिस्टर फूल",
    "द मूर्ख": "द फूल",
    "श्री": "मिस्टर",

    # ---- second batch, verified against 32 chapters of real output ----
    # Excluded on evidence: भोग (substring of संभोग), संयम (substring of
    # संयमित), माता/माँ/मां (121 ordinary uses; breaks माता-पिता), and bare
    # कोहरा (ordinary fog). Only compounds and proper nouns are listed.
    "इच्छा के मातृ वृक्ष": "मदर ट्री ऑफ डिज़ायर",
    "इच्छा का मातृ वृक्ष": "मदर ट्री ऑफ डिज़ायर",
    "कामना का मातृ वृक्ष": "मदर ट्री ऑफ डिज़ायर",
    "मातृ वृक्ष": "मदर ट्री",
    "मातृवृक्ष": "मदर ट्री",
    "महान माता": "ग्रेट मदर",
    "महान माँ": "ग्रेट मदर",
    "महान मां": "ग्रेट मदर",
    "महामाता": "ग्रेट मदर",
    "भूरा कोहरा": "ग्रे फॉग",
    "धूसर कोहरा": "ग्रे फॉग",
    "ग्रे कोहरा": "ग्रे फॉग",
    "स्लेटी कोहरा": "ग्रे फॉग",
    "एमोन": "अमोन",
    "शिकारी": "हंटर",
}

STYLE_RULES = {
    "devanagari": (
        "Write the translation entirely in Devanagari script. "
        "Transliterate proper nouns into Devanagari; do not leave them in Latin script."
    ),
    "codemixed": (
        "Write in natural Hinglish as an educated Indian reader would speak it. "
        "Keep common English nouns, proper nouns and technical terms in Latin "
        "script; write the connecting grammar in Devanagari."
    ),
    # For an audiobook the script is invisible to the listener - only
    # pronunciation matters - and IndicF5 is trained on Indic scripts, so
    # Latin-script words risk being mispronounced. This style gets the casual
    # register of `codemixed` while keeping everything in Devanagari.
    "spoken": (
        "Write in everyday conversational Hindi - the way an educated Indian "
        "narrator would actually tell this story out loud to a friend. Do NOT "
        "use literary, formal or Sanskritised vocabulary. Whenever an English "
        "word is what people normally say in daily speech, keep that English "
        "word instead of reaching for a Sanskrit equivalent, but spell it in "
        "Devanagari so it reads aloud correctly. For example: write यूनीक, "
        "not अद्वितीय; प्रोजेक्शन, not प्रक्षेपण; मिरर, not दर्पण; पावर, not शक्ति. "
        "Everything must still be in Devanagari script - no Latin letters."
    ),
}


def load_glossary(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _glossary_block(glossary: dict[str, str], text: str, limit: int = 60) -> str:
    """Only include terms that actually appear in this block.

    A 400-entry glossary in every prompt would eat the context window and
    dilute the model's attention. Filtering to present terms keeps prompts
    small and noticeably improves adherence.
    """
    hits = [(en, hi) for en, hi in glossary.items()
            if re.search(rf"\b{re.escape(en)}\b", text, re.IGNORECASE)]
    if not hits:
        return ""
    lines = "\n".join(f"{en} -> {hi}" for en, hi in hits[:limit])
    return (
        "\nUse these fixed translations for recurring names and terms. "
        "Inflect them naturally for Hindi grammar, but do not substitute "
        "different words:\n" + lines
    )


def split_blocks(text: str, words_per_block: int) -> list[str]:
    """Split on paragraph boundaries, never mid-sentence."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks, cur, count = [], [], 0
    for p in paras:
        w = len(p.split())
        if cur and count + w > words_per_block:
            blocks.append("\n\n".join(cur))
            cur, count = [], 0
        cur.append(p)
        count += w
    if cur:
        blocks.append("\n\n".join(cur))
    return blocks


def build_messages(block: str, prev_hindi: str, glossary: dict, cfg: dict) -> list[dict]:
    """Build the prompt EXACTLY as sarvam-translate's model card specifies.

    This model is a translator, not an instruction-following chat model: it
    translates whatever it is given, including the system message. An earlier
    version of this function put style rules, a glossary table and rolling
    context in the system prompt - all of which came back translated into
    Hindi and prepended to every chapter, so the narrator read the
    instructions aloud before starting the story.

    The model card allows exactly one system string: "Translate the text
    below to {lang}." Register and terminology are therefore NOT steerable
    here; they are applied afterwards by apply_glossary() on the Hindi output.
    `prev_hindi` is accepted for signature compatibility and deliberately
    unused - the model is document-level and handles intra-block context
    itself.
    """
    return [
        {"role": "system", "content": "Translate the text below to Hindi."},
        {"role": "user", "content": block},
    ]


def apply_glossary(hindi: str, glossary: dict[str, str]) -> str:
    """Force preferred Hindi terms onto the model's output.

    The prompt cannot carry a term list (see build_messages), so terminology
    is enforced here instead. Only Hindi->Hindi substitutions in `casual` are
    applied: the model's formal/tatsam choice is swapped for the everyday word.

    This is safe precisely because the replacements are English loanwords and
    common tadbhav nouns, which do not inflect in Hindi - मिरर is मिरर in
    "मिरर के" and "मिरर में" alike. It would NOT be safe for verbs or
    adjectives that take case endings.
    """
    # Longest first: "आदिम राक्षसी" must be consumed before bare "राक्षसी",
    # otherwise the compound is broken apart and can never match.
    for formal in sorted(CASUAL_SUBSTITUTIONS, key=len, reverse=True):
        hindi = hindi.replace(formal, CASUAL_SUBSTITUTIONS[formal])
    return hindi


# A glossary row echoed back by the model looks like "term -> term" on its own
# short line. Real narrative prose does not.
_LEAK_ROW = re.compile(r"^.{0,60}\s->\s.{0,60}$")
_LEAK_PROSE = (
    "रोजमर्रा की बातचीत वाली हिंदी में लिखें",
    "इन निश्चित अनुवादों का उपयोग",
    "देवनागरी लिपि में होना चाहिए",
    "पिछला अंश इस प्रकार समाप्त",
    "अनुवाद के अलावा कोई प्रस्तावना",
)


def strip_prompt_leakage(hindi: str) -> str:
    """Remove prompt text the model echoed into its output.

    sarvam-translate translates whatever it is handed, so anything in the
    prompt comes back as Hindi in the output - once per block, which means
    these fragments appear at every block boundary, not just the start.

    build_messages no longer puts anything steerable in the prompt, so on new
    runs this should be a no-op. It stays as a regression net, because the
    failure is silent: the text still reads as plausible Hindi and only shows
    up when someone listens to the audiobook.
    """
    kept = []
    for para in hindi.split("\n"):
        s = para.strip()
        if not s:
            continue
        if len(s) < 80 and _LEAK_ROW.match(s):
            continue
        if any(m in s for m in _LEAK_PROSE):
            continue
        kept.append(s)
    return "\n\n".join(kept)


class HFOffline:
    """Kaggle path. Plain transformers + bitsandbytes 4-bit, not vLLM.

    vLLM was tried first: it hard-rejects float16 for Gemma3 (sarvam-translate's
    architecture) as numerically unstable, T4 (compute capability 7.5) has no
    bfloat16 hardware support, dense float32 needs ~16GB (too much for one
    T4), and splitting across both T4s via tensor_parallel_size=2 hits a
    documented vLLM bug where Gemma3's tied embedding/lm_head isn't actually
    sharded (vllm-project/vllm#45719) - each rank still needs most of the
    dense model regardless. Plain transformers has none of these
    architecture-specific guards, and 4-bit quantization (~2GB weights)
    fits comfortably on a single T4 without any of the above.
    """

    def __init__(self, cfg: dict):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        self.tokenizer = AutoTokenizer.from_pretrained(cfg["model"])
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        # Left padding: with right padding a batched generate() continues from
        # pad tokens instead of the last real token, producing garbage.
        self.tokenizer.padding_side = "left"

        # 4bit costs speed - every weight is dequantised on each forward pass -
        # but only needs ~2GB. fp16 needs ~8GB, which still fits a 15GB T4 and
        # avoids that overhead. Gemma3 has known fp16 instability (vLLM refuses
        # it outright), so fp16 output must be eyeballed before it is trusted.
        kw = {"device_map": "auto", "attn_implementation": "sdpa"}
        if cfg.get("precision", "4bit") == "fp16":
            kw["torch_dtype"] = torch.float16
        else:
            kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_quant_type="nf4",
            )
        self.model = AutoModelForCausalLM.from_pretrained(cfg["model"], **kw)
        self.model.eval()
        self.temperature = cfg["temperature"]

    def chat_batch(self, batch: list[list[dict]]) -> list[str]:
        """Translate several prompts in one forward pass.

        Blocks inside one chapter must stay sequential (each needs the previous
        block's Hindi as rolling context), but block N of chapter A is
        independent of block N of chapter B - so chapters can be run in
        lockstep and batched here, which is where the GPU actually gets used.
        """
        import torch
        texts = [
            self.tokenizer.apply_chat_template(
                m, add_generation_prompt=True, tokenize=False
            )
            for m in batch
        ]
        enc = self.tokenizer(
            texts, return_tensors="pt", padding=True, add_special_tokens=False
        ).to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                **enc,
                max_new_tokens=3000,
                do_sample=self.temperature > 0,
                temperature=self.temperature or None,
                pad_token_id=self.tokenizer.pad_token_id,
            )
        n_in = enc["input_ids"].shape[1]
        return [
            self.tokenizer.decode(row[n_in:], skip_special_tokens=True).strip()
            for row in out
        ]

    def chat(self, messages: list[dict]) -> str:
        import torch
        # return_dict=True explicitly: depending on transformers version,
        # apply_chat_template returns either a bare tensor or a BatchEncoding
        # dict. Relying on the default got us a BatchEncoding and a
        # KeyError('shape') crash. Ask for the dict and unpack it - which
        # also hands generate() the attention_mask, as it wants.
        enc = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True,
            return_tensors="pt", return_dict=True,
        ).to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                **enc,
                max_new_tokens=3000,
                do_sample=self.temperature > 0,
                temperature=self.temperature or None,
            )
        text = self.tokenizer.decode(
            out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True
        )
        return text.strip()


class OpenAICompatible:
    """CPU fallback. Point at llama-server running a Q4 GGUF.

    llama-server -m sarvam-translate-Q4_K_M.gguf -c 8192 --port 8000
    Expect roughly 5 tok/s on a Ryzen 3 3100. Usable overnight, not in a batch.
    """

    def __init__(self, cfg: dict):
        from openai import OpenAI
        self.client = OpenAI(base_url=cfg["base_url"], api_key="none")
        self.model = cfg.get("served_model", "sarvam-translate")
        self.temperature = cfg["temperature"]

    def chat(self, messages: list[dict]) -> str:
        r = self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=self.temperature, max_tokens=3000,
        )
        return r.choices[0].message.content.strip()


def make_backend(cfg: dict):
    if cfg["backend"] == "hf_offline":
        return HFOffline(cfg)
    if cfg["backend"] == "openai":
        return OpenAICompatible(cfg)
    raise ValueError(f"unknown translate backend: {cfg['backend']}")


def translate_chapters_batched(texts: list[str], backend, glossary: dict,
                               cfg: dict) -> list[str]:
    """Translate several chapters at once by running them in lockstep.

    Blocks inside one chapter cannot be parallelised - but block i of chapter A
    is independent of block i of chapter B, so all the chapters' i-th blocks
    go through generate() as a single batch. That is what actually keeps the
    GPU busy; one-block-at-a-time leaves it mostly idle between tokens.

    Chapters differ in length, so each round only includes the chapters that
    still have an i-th block.
    """
    per_chapter = [split_blocks(t, cfg["words_per_block"]) for t in texts]
    out: list[list[str]] = [[] for _ in texts]
    rounds = max((len(b) for b in per_chapter), default=0)

    for i in range(rounds):
        active = [j for j, blocks in enumerate(per_chapter) if i < len(blocks)]
        msgs = [build_messages(per_chapter[j][i], "", glossary, cfg) for j in active]
        results = backend.chat_batch(msgs)
        for j, hindi in zip(active, results):
            out[j].append(apply_glossary(strip_prompt_leakage(hindi), glossary))
        print(f"    block {i + 1}/{rounds} across {len(active)} chapters", flush=True)

    return ["\n\n".join(blocks) for blocks in out]


def translate_chapter(text: str, backend, glossary: dict, cfg: dict) -> str:
    blocks = split_blocks(text, cfg["words_per_block"])
    out: list[str] = []
    prev = ""
    for i, block in enumerate(blocks, 1):
        hindi = backend.chat(build_messages(block, prev, glossary, cfg))
        hindi = apply_glossary(hindi, glossary)
        print(f"    block {i}/{len(blocks)}  {len(hindi)} chars", flush=True)
        out.append(hindi)
        prev = hindi
    return "\n\n".join(out)
