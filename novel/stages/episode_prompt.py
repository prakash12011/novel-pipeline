"""Write the Gemini-app prompt for an episode's Hinglish narration script.

You give Gemini this prompt together with the episode's English chapters (as sources or
pasted). The reply goes to `episode_translate draft`. The prompt is the same for every
episode: only the chapter range and the name list change, and the name
list comes from the episode's glossary.json, so names stay spelled the same way in
every episode.

    python -m abpipe.episode_prompt --episode 1
    # -> workspace/episodes/E00N/gemini-prompt.txt  (paste it into Gemini)
"""

from __future__ import annotations

import argparse
import json

from . import episode as E

TEMPLATE = """You are writing the narration script for a Hinglish YouTube story-recap video, made from the attached chapters {first} to {last} of the novel "Circle of Inevitability". Friends and family will listen. It must be gripping, easy to follow, and sound like a person telling a story to a friend.

WHAT TO KEEP AND CUT
- Keep every plot event, reveal, decision, mystery clue, and the lines of dialogue that matter. Add nothing that is not in the chapters.
- Cut physical appearance descriptions (clothes, hair, faces): the video shows them. Cut repeated inner thoughts, filler and slow description.
- No word limit. Each chapter is as long as its story needs: a quiet chapter can be short, a chapter full of lore, plans or action runs long. Never drop a plot point, clue or explanation to make a chapter shorter. Retell and humanise in one go; do not summarise in a first pass and expand later.
- ACTION SCENES (fights, chases, traps, escapes, ambushes, magic or powers in use): never summarise them. Tell them blow by blow, in very fine detail, as if narrating it live: where everyone stands, what each person does next and why, which weapon, trick, trap or power they use, how the enemy reacts, what goes wrong, the near misses, the plan that works or fails, and exactly how it ends. One move per sentence, in the order it happens, so a listener can picture every step. Include wounds, blood, gore, and all graphic details to keep the story authentic. These are the most exciting parts of the story, so they get the most words.
- Sexual content and nudity: censor these scenes. Say plainly what happened in one short sentence with no physical detail. Skip graphic descriptions of intimate acts.

HOW IT SOUNDS
- Everyday Hinglish, the way educated Indians talk with friends: Hindi grammar, English words where people really use them (टाइम, प्लान, प्रॉब्लम, आइडिया, रूम). Not literary or Sanskritised Hindi.
- Storyteller voice: short spoken sentences, mostly present tense, a natural "तो...", "अब देखो...", "सोचो..." now and then (not every line). Dialogue is retold by the narrator ("लुमियान कहता है कि ..."), with no quotation marks.
- Every paragraph is one idea in two or three short sentences, about 150 to 250 characters. Never one long block.
- End each chapter on a small hook that makes the listener want the next one.
- Write numbers as Hindi words: तीन दिन, दो ब्रेड, तेरह साल. Never digits.
- Devanagari script only, no Latin letters at all: a text-to-speech narrator reads it.

NAMES - always spell exactly like this, every time, in every chapter (English in brackets is only for you):
{names}
A name that is not in this list: spell it the way it sounds in Devanagari, once, and then keep that spelling identical everywhere.

FORMAT - exactly this, nothing else:
- Each chapter starts with a heading line: चैप्टर <the novel's chapter number in Hindi words>: <title in Hinglish>
- Then the narration in short paragraphs separated by one blank line.
- In chapter replies: no notes, no headings other than the chapter lines, no explanations, no English, no summary at the end.

Write the chapters in order, one after the other. If you run out of room, stop at the end of a finished chapter, and when I say "continue", carry on with the next chapter in the same way, with no recap.

SECOND JOB - THE CAST SHEET. Do this only when I say "cast sheet" (normally after the last chapter). Then reply with ONE JSON object and nothing else: no code fence, no notes. Use chapters {first} to {last} only. The artists who draw the pictures work from this, so it must be exact.
{{
  "characters": {{
    "<snake_case_key>": {{
      "display": "the name as the novel spells it",
      "flow_name": "the same as display, or null",
      "hi": "the Devanagari spelling from the NAMES list above (or the one you used in the narration)",
      "tier": 1,
      "minor": false,
      "chapters": "26,31-32",
      "appearance": "one English phrase for an illustrator",
      "invented": "the parts of the appearance the novel does not give, or the word none",
      "role": "one English sentence"
    }}
  }},
  "outfits": {{
    "<character key>": {{ "22-24,34-35 dream ruins": "what they wear, carry or look like in those chapters" }}
  }},
  "content_flags": {{
    "<chapter or range, e.g. 23-24>": "what that chapter shows that a picture must soften"
  }}
}}
- "characters": every person, creature or spirit who appears in chapters {first}-{last} and is NOT in the cast listed below. Anyone already in the cast is not repeated.
- "chapters": every chapter the character is present in, written as a list of numbers and ranges.
- "tier": 1 for a lead, 2 for someone in three or more chapters, 3 for someone in one or two chapters.
- "flow_name": the display name for someone who appears in three or more chapters, or for a creature or spirit with a fixed unusual look; null for people who appear in only one or two chapters.
- "minor": true for anyone under eighteen. They are always drawn fully clothed.
- "appearance": for an artist: age, build, hair, eyes, clothes, in one phrase, like "village farmer in his forties with a weathered face and short dark hair, wearing plain dark work clothes". No names, no story, no nudity or sexual detail.
- "outfits": for any character, new or already in the cast, only for the chapters where what they wear, carry or look like differs from their usual look. The key is the chapter numbers, then a short label.
- "content_flags": for each chapter that shows violence, gore, body horror, a corpse, nudity, or sexual or adult themes, say in one sentence what happens. Leave out chapters with nothing to soften.

ALREADY IN THE CAST (do not list them again; "outfits" may use their keys):
{known}
"""


def names_for(episode: int) -> dict:
    """English -> Devanagari spellings the narrator must use.

    names.json in the episode folder (edit it to add names), else the nearest earlier
    episode's, so a later episode starts with every spelling already approved. The
    audiobook glossary is not used: it lists names from chapters 800+ and everyday words.
    """
    for n in range(episode, 0, -1):
        path = E.root(n) / "names.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise SystemExit(f"no names.json in episode {episode} or earlier: create "
                     f"{E.root(episode) / 'names.json'} as {{\"English\": \"देवनागरी\"}}")


def known_cast(episode: int) -> str:
    """The roster keys the cast sheet must not repeat: this episode's bible, else the nearest earlier one."""
    for n in range(episode, 0, -1):
        path = E.root(n) / "bible" / "characters.json"
        if path.exists():
            cast = json.loads(path.read_text(encoding="utf-8"))["characters"]
            return chr(10).join(f"{k} = {c['display']} (chapters {c['chapters']})" for k, c in cast.items())
    return "(nobody yet: this is the first episode)"


def build(episode: int) -> str:
    root = E.root(episode)
    if not (root / "episode.json").exists():
        raise SystemExit(f"episode {episode} isn't set up: python -m abpipe.episode init --episode {episode} --start <first chapter> --end <last chapter>")
    info = E.info(root)
    first, last = info.get("start", 1), info.get("end", info.get("start", 1) + E.CHAPTERS_PER_EPISODE - 1)
    names = names_for(episode)
    names = "\n".join(f"{hi} ({en})" for en, hi in names.items())
    return TEMPLATE.format(first=first, last=last, names=names, known=known_cast(episode))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episode", type=int, required=True)
    args = ap.parse_args()
    text = build(args.episode)
    out = E.root(args.episode) / "gemini-prompt.txt"
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
