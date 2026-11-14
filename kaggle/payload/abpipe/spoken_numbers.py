"""Digits -> English number words, spelled in Devanagari, for the narrator.

sarvam-translate leaves numbers as digits - "सीक्वेंस 7", "अध्याय 946:",
"0-01", "कमरे 2303" - and IndicF5 has no reading for a bare digit. It is
trained on spelled-out Indic text, so "7" comes out as a short noise rather
than a number. Every chapter opens with one (the "अध्याय N:" heading), and the
first 305 translated chapters hold 1,659 digit runs between them.

The numbers are read in ENGLISH (7 -> सेवन, not सात), matching how the book's
own terms are already kept (सीक्वेंस, पाथवे, मिरर). They are spelled in
Devanagari for the same reason the glossary does it: IndicF5 mispronounces
Latin script. No nukta (फाइव, जीरो), the way these loanwords are usually
written in everyday Hindi (फोन, फाइल).

Applied at synthesis time, not translation time: hindi/*.txt keeps its digits,
and chapters translated before this existed are covered too. Paragraph breaks
are never touched, so timing-map paragraph indices still line up with the
English source. timing.estimate() deliberately does NOT apply this - it
rebuilds maps for audio that was narrated from the raw text.

Preview what a chapter will sound like before spending GPU time on it:
    python -m abpipe.spoken_numbers workspace/hindi/00144.txt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ONES = ["जीरो", "वन", "टू", "थ्री", "फोर", "फाइव", "सिक्स", "सेवन", "एट", "नाइन",
        "टेन", "इलेवन", "ट्वेल्व", "थर्टीन", "फोर्टीन", "फिफ्टीन", "सिक्सटीन",
        "सेवनटीन", "एटीन", "नाइनटीन"]
TENS = ["", "", "ट्वेंटी", "थर्टी", "फोर्टी", "फिफ्टी", "सिक्सटी", "सेवेंटी",
        "एटी", "नाइंटी"]
SCALES = [(10**9, "बिलियन"), (10**6, "मिलियन"), (1000, "थाउजेंड")]

# An ordinal is its cardinal with the LAST word swapped:
# 23 -> ट्वेंटी थ्री -> ट्वेंटी थर्ड.
ORDINAL = {
    "जीरो": "जीरोथ", "वन": "फर्स्ट", "टू": "सेकंड", "थ्री": "थर्ड",
    "फोर": "फोर्थ", "फाइव": "फिफ्थ", "सिक्स": "सिक्स्थ", "सेवन": "सेवंथ",
    "एट": "एट्थ", "नाइन": "नाइंथ", "टेन": "टेंथ", "इलेवन": "इलेवंथ",
    "ट्वेल्व": "ट्वेल्फ्थ", "थर्टीन": "थर्टींथ", "फोर्टीन": "फोर्टींथ",
    "फिफ्टीन": "फिफ्टींथ", "सिक्सटीन": "सिक्सटींथ", "सेवनटीन": "सेवनटींथ",
    "एटीन": "एटींथ", "नाइनटीन": "नाइनटींथ", "ट्वेंटी": "ट्वेंटिएथ",
    "थर्टी": "थर्टिएथ", "फोर्टी": "फोर्टिएथ", "फिफ्टी": "फिफ्टिएथ",
    "सिक्सटी": "सिक्सटिएथ", "सेवेंटी": "सेवेंटिएथ", "एटी": "एटिएथ",
    "नाइंटी": "नाइंटिएथ", "हंड्रेड": "हंड्रेडथ", "थाउजेंड": "थाउजेंडथ",
    "मिलियन": "मिलियनथ", "बिलियन": "बिलियनथ",
}

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# A letter that must not be glued to a number word: Latin, or Devanagari
# minus the dandas (0964-5), digits (0966-F) and abbreviation sign (0970).
# "बी1" has to become "बी वन", not "बीवन".
_LETTER = re.compile(r"[A-Za-zऀ-ॣॱ-ॿ]")
_NOT_AFTER_LETTER = r"(?<![A-Za-zऀ-ॣॱ-ॿ])"

# Western (30,000) or Indian (1,00,000) grouping; the commas are dropped. A
# comma followed by a space is punctuation, not grouping: "बिल्डिंग 5, 23 वीं".
_NUM = r"(?:\d{1,3}(?:,\d{2,3})*,\d{3}(?!\d)|\d+)"

_MONTHS = ("जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|सितम्बर|अक्टूबर|"
           "अक्तूबर|नवंबर|नवम्बर|दिसंबर|दिसम्बर")


def _below_thousand(n: int) -> list[str]:
    words = []
    if n >= 100:
        words += [ONES[n // 100], "हंड्रेड"]
        n %= 100
    if n >= 20:
        words.append(TENS[n // 10])
        n %= 10
    if n:
        words.append(ONES[n])
    return words


def cardinal(n: int) -> list[str]:
    """946 -> नाइन हंड्रेड फोर्टी सिक्स. No "and": एंड mid-number reads as a
    separate word to a Hindi listener."""
    if n == 0:
        return [ONES[0]]
    words = []
    for value, name in SCALES:
        if n >= value:
            words += _below_thousand(n // value) + [name]
            n %= value
    return words + _below_thousand(n)


def ordinal(s: str) -> list[str]:
    s = s.replace(",", "")
    if len(s) > 12:
        return digits(s)
    words = cardinal(int(s))
    words[-1] = ORDINAL[words[-1]]
    return words


def digits(s: str) -> list[str]:
    return [ONES[int(c)] for c in s]


def number(s: str) -> list[str]:
    """A bare number as written. A leading zero means a code, not a quantity -
    007, or the 01 in sealed artifact 0-01 - so it is read digit by digit."""
    s = s.replace(",", "")
    # Past 12 digits it is a code or an ID; cardinal() also stops at billions.
    if (len(s) > 1 and s[0] == "0") or len(s) > 12:
        return digits(s)
    return cardinal(int(s))


def paired(s: str) -> list[str]:
    """Room numbers and years, read in pairs the English way:
    2303 -> ट्वेंटी थ्री ओ थ्री, 702 -> सेवन ओ टू, 1368 -> थर्टीन सिक्सटी एट."""
    head, tail = cardinal(int(s[:-2])), s[-2:]
    if tail == "00":
        return head + ["हंड्रेड"]
    if tail[0] == "0":
        return head + ["ओ", ONES[int(tail[1])]]
    return head + cardinal(int(tail))


def year(s: str) -> list[str]:
    # 2005 is "two thousand five", not "twenty oh five"; 1905 is "nineteen oh five".
    return cardinal(int(s)) if s[1] == "0" else paired(s)


def clock(h: str, m: str) -> list[str] | None:
    hh, mm = int(h), int(m)
    if hh > 24 or mm > 59:
        return None
    if mm == 0:
        # 4:00 -> four o'clock; 17:00 -> seventeen hundred.
        return cardinal(hh) + (["ओ", "क्लॉक"] if hh <= 12 else ["हंड्रेड"])
    if m[0] == "0":
        return cardinal(hh) + ["ओ", ONES[mm]]
    return cardinal(hh) + cardinal(mm)


# (pattern, words) in the order they run. Each rule consumes its digits, so a
# later, more general rule never sees them. `words` returns None to leave a
# match alone. Groups named "pre" are context words re-emitted unchanged.
_RULES = [
    # 4:30, 05:47, 21:50 - story timestamps and clock times
    (r"(?<![\d:.,])(?P<h>\d{1,2}):(?P<m>\d{2})(?![\d:])",
     lambda m: clock(m["h"], m["m"])),
    # 6.50 बजे - a clock time written with a dot
    (r"(?<![\d.])(?P<h>\d{1,2})\.(?P<m>\d{2})(?=\s*बजे)",
     lambda m: clock(m["h"], m["m"])),
    # 1.75 मीटर - decimals read digit by digit after the point
    (rf"(?<![\d.,])(?P<n>{_NUM})\.(?P<f>\d+)(?![\d.])",
     lambda m: number(m["n"]) + ["पॉइंट"] + digits(m["f"])),
    (rf"(?P<n>{_NUM})\s?%", lambda m: number(m["n"]) + ["परसेंट"]),
    # 23 वीं मंजिल -> ट्वेंटी थर्ड मंजिल. The Hindi suffix is dropped.
    (rf"(?P<n>{_NUM})\s?(?:वीं|वें|वां|वाँ)(?![ऀ-ॿ])",
     lambda m: ordinal(m["n"])),
    (rf"{_NOT_AFTER_LETTER}(?P<pre>(?:वर्ष|साल|सन्|सन|ईयर|{_MONTHS}),?\s+)"
     r"(?P<n>1\d{3}|20\d{2})(?![\d,])",
     lambda m: year(m["n"])),
    (rf"{_NOT_AFTER_LETTER}(?P<pre>(?:कमरा|कमरे|कमरों|रूम|अपार्टमेंट|फ्लैट)\s+"
     r"(?:नंबर\s+)?)(?P<n>[1-9]\d{2,3})(?![\d,])",
     lambda m: paired(m["n"])),
    (rf"(?P<n>{_NUM})", lambda m: number(m["n"])),
]
_RULES = [(re.compile(p), fn) for p, fn in _RULES]


def _apply(rx: re.Pattern, fn, text: str) -> str:
    def repl(m: re.Match) -> str:
        words = fn(m)
        if words is None:
            return m.group()
        out = " ".join(words)
        pre = m.groupdict().get("pre") or ""
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if not pre and before and _LETTER.match(before):
            out = " " + out
        if after and _LETTER.match(after):
            out += " "
        return pre + out
    return rx.sub(repl, text)


def speakable(text: str) -> str:
    """Rewrite every digit in `text` as English number words in Devanagari."""
    text = text.translate(_DEVANAGARI_DIGITS)
    for rx, fn in _RULES:
        text = _apply(rx, fn, text)
    return text


# ---- Hindi number WORDS ---------------------------------------------------
#
# Gemini is told to write numbers as digits, and mostly does; the ones it
# writes as Hindi words (सात सेफिरात, तीन सिरों) would be narrated in Hindi.
# Only words with no other meaning are replaced automatically. एक is also "a",
# दो also "give", शून्य also "void", and the plural forms (चारों ओर = "all
# around", दोनों = "both") are idioms - those are only reported, for a repair
# round or a human. Whole words only: Devanagari has no reliable \b after a
# vowel sign, so the boundaries are explicit lookarounds.

HINDI_SAFE = {
    "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "सात": 7,
    "आठ": 8, "नौ": 9, "दस": 10, "ग्यारह": 11, "बारह": 12, "तेरह": 13,
    "चौदह": 14, "पंद्रह": 15, "पन्द्रह": 15, "सोलह": 16, "सत्रह": 17,
    "अठारह": 18, "उन्नीस": 19, "बीस": 20, "पच्चीस": 25, "पचीस": 25,
    "तीस": 30, "चालीस": 40, "पचास": 50, "साठ": 60, "सत्तर": 70,
    "अस्सी": 80, "नब्बे": 90,
}
HINDI_SCALE = {"सौ": "हंड्रेड", "हजार": "थाउजेंड", "हज़ार": "थाउजेंड"}
_D = r"ऀ-ॿ"
_SAFE_RX = re.compile(rf"(?<![{_D}])({'|'.join(sorted(HINDI_SAFE, key=len, reverse=True))})"
                      rf"(?:\s+({'|'.join(HINDI_SCALE)}))?(?![{_D}])")
_SCALE_ALONE_RX = re.compile(rf"(?<![{_D}])({'|'.join(HINDI_SCALE)})(?![{_D}])")
# दो as the number: followed by a word, and not the verb "give/let" - that comes
# after an infinitive (जाने दो), दे, मत, or at the end of a clause.
_DO_NUMBER_RX = re.compile(rf"(?<![{_D}])(?<!ने )(?<!दे )(?<!मत )दो(?=\s+[{_D}])(?![{_D}])")


# Idioms that happen to be made of number words. नौ दो ग्यारह होना = to run off.
IDIOMS = ("नौ दो ग्यारह",)


def _without_idioms(text: str) -> str:
    for idiom in IDIOMS:
        text = text.replace(idiom, "")
    return text


def hindi_number_words(text: str) -> list[str]:
    """Number words a listener would hear in Hindi, for repair or review."""
    text = _without_idioms(text)
    found = [m.group(0) for m in _SAFE_RX.finditer(text)]
    rest = _SAFE_RX.sub(" ", text)          # a scale already counted with its number
    found += [m.group(0) for m in _SCALE_ALONE_RX.finditer(rest)]
    found += ["दो"] * len(_DO_NUMBER_RX.findall(text))
    return found


def english_number_words(text: str) -> str:
    """Replace the unambiguous Hindi number words with their English reading.

    तीन -> थ्री, पांच सौ -> फाइव हंड्रेड. A scale on its own is "a hundred" /
    "a thousand" (सौ लोग). Ambiguous words are left alone - see above.
    """
    def safe(m: re.Match) -> str:
        words = cardinal(HINDI_SAFE[m.group(1)])
        return " ".join(words + ([HINDI_SCALE[m.group(2)]] if m.group(2) else []))

    def convert(part: str) -> str:
        part = _SAFE_RX.sub(safe, part)
        return _SCALE_ALONE_RX.sub(lambda m: "वन " + HINDI_SCALE[m.group(1)], part)
    # Split around idioms so they come back untouched.
    pieces = re.split("(" + "|".join(map(re.escape, IDIOMS)) + ")", text)
    return "".join(p if p in IDIOMS else convert(p) for p in pieces)


def main() -> None:
    """Print each line that changes, before and after."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m abpipe.spoken_numbers <hindi.txt>")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
        spoken = speakable(line)
        if spoken != line:
            print(f"- {line}\n+ {spoken}\n")


if __name__ == "__main__":
    main()
