# Issues we hit, and what fixed them

A reference for the picture and translation pipeline: each problem we met while moving from Google Flow to Qwen-Image
on Kaggle (November 2026, COI chapter 1), what caused it and what we did. **Status**: fixed, partly fixed, or open.

## Translation and recap

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Pictures repeated one place; fewer distinct scenes than before | The translation squeezed Lumian's told story into ~13 windows and stretched the tavern talk to ~35. Picture lines follow the windows, so one window held 3 events and the morgue came 6 times in a row. Not the image prompt: the model, seeds and sheets were identical. | Narration rules: open by setting the scene (where, when, who, by name); a story a character tells is retold beat by beat, never one summary line; talk around it stays short. Target 900 words (band 720-1080, about half of a 2,000-word chapter). | Fixed (chapter 1: 68 windows, the story over ~32) |
| A chapter heading line was narrated | The narration started with "चैप्टर एक: ..." | No heading line in the narration (`heading.strip`, `novel strip-heading`) | Fixed |
| Formal / pure Hindi creeping in | | `HINGLISH` rule: common daily-life Hindi, never shuddh (पानी not जल, घर not निवास) | Fixed |
| Every chapter 1 translation needed a 2nd call | The name check demands every English name appear in Hindi; the model drops the minor place "Bigorre" (बिगोर) every time | none yet | Open: relax the check for names the model may leave out |
| Recap too long, or never the right length | 20-25% of an episode is ~2,000 words; one call returned 200-500 words or the whole text; per-chapter calls overshot by 10% | Recap is one call per episode after the translation: 60-90 words (30-40 s), 4-6 short paragraphs, `<episode>/recap.txt`; a model overshoot margin is accepted | Fixed |
| Retranslation refused | Episode marked `published: true` | Set `published: false` in `episode.yaml` before, back to `true` after the re-upload | Remember for E001 |

## Gemini API

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Bad picture lines: a told story became 22 empty morgue pictures; tavern opening lost | gemini-3.5-flash-lite cannot follow the told-story rule | `gemini.api_model: gemini-4-hydrogen` (user settings) | Fixed |
| Calls failing for minutes | 429 on all three free keys after a busy day; key 3 dropped long requests ("connection forcibly closed", ~3 min into a reply) | `GEMINI_API_KEY_FOR_HYDROGEN` tried first; OpenRouter fallback (`google/gemini-4-hydrogen`, ~$0.10 a chapter) when every key is limited or a request drops 3 times | Fixed |
| A reasoning model returned an empty text | `max_tokens` 20 was spent on thinking | The fallback allows 32k output tokens | Fixed |
| Keys in the repo `.env` were not used | The pipeline reads keys only from `~/.novel-pipeline/secrets.env` | Copy keys there | Note |

## Picture lines (what each picture shows)

| Problem | Cause | Fix | Status |
|---|---|---|---|
| While Lumian tells a story, the tavern is shown | The scene writer drew the room it is told in | Qwen rule: a told event is drawn itself, in its own group and place, one new moment per picture; the telling room only for listeners, the opening and the end | Fixed |
| Morgue pictures described as "Lumian's room" | The bible had no morgue; the group borrowed the nearest place and its description | Story places added to the bible (city street, rented room, hospital morgue and corridor, gray fog); in production the scene call may add places | Fixed |
| The same face 4-5 pictures in a row | Weak moods ("calm", "focused") and the sheet's neutral face won | Rule: every expression names mouth, eyes and brows, differs from the person's previous picture, never only a feeling word. Prompt: `face: ...` inside the person's block | Fixed |
| A person named in the action but not in the cast | "...if Ryan Koss wants to pay" while Ryan is not in the picture | Rule: action and others never name a bible person outside the cast | Fixed (rule) |

## Qwen pictures

| Problem | Cause | Fix | Status |
|---|---|---|---|
| White specks in the eyes | Model habit | Closing line "Clean eyes, no white specks" (the negative prompt is ignored at cfg 1) | Fixed |
| A nameless extra drawn with the main character's face (2nd Lumian pushing the trolley, s12) | The extra had no look, and the only face the model had was the sheet | Rule: every extra person gets a look of their own (age, build, hair, clothes); reroll fixed s12 | Fixed |
| Two named people: one sheet drawn twice (Lumian x3, Pierre x2), or one person given the other's face (the barman with Lumian's face and Maurice's red nose) | Two references in one picture confuse Qwen; Lumian, Ryan and Maurice all have black or dark hair and blue eyes | New prompt format: one block per person `[WHERE: Name (<ref>) - look; pose; hands; face]`, a "TWO DIFFERENT people ... X is the ..., Y is the ..." contrast line, extras smaller in the background, instruction sentences removed (prompts 25% shorter) | Being tested (same seeds, 10 pictures). If not enough: automatic check and redraw |
| Prompt sentences like "appears only once" had little effect | Qwen follows visual description better than instructions | Removed; said visually instead | Fixed |
| The `qwen_charsheet.py` script was lost | It lived in a cleared temp folder | Rebuilt as `novel ktrial charsheet`; sheets moved to `bible/qwen/`; `novel images` makes missing sheets first | Fixed |
| A special outfit has no Qwen sheet | Sheets are base looks only | Falls back to the Flow portrait in `bible/flow/` | Open |
| A bible person without a Flow name gets no reference | `kprompts` attaches references only to people with `flow_name` | none | Open |

## Kaggle, runs and files

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Image server and narration fighting for one account | One account was set by hand for both | `kaggle.pick_free`: each job takes the first of prakash, seema, shashi with nothing running; the image server's account gets the ngrok token dataset when it lacks it | Fixed |
| Only one image server at a time | A free ngrok account opens one tunnel | One server; start it on any free account | By design |
| New server's tunnel "offline" (ERR_NGROK_3200) on prakash, notebook ERROR after the probes | Specific to the prakash account: twice in a row with nothing else running, while seema with the same ngrok token and domain worked right after. ComfyUI itself started; the ngrok endpoint never came online. Cause not found yet | `images.kaggle_profile: seema` (user settings) so the picker tries seema first | Open: find why prakash's tunnel fails |
| The server was gone after a pause | It stops itself after 20 idle minutes; a restart takes 7-10 minutes | Start pictures soon after `up`, or keep work queued | Note |
| `--limit 10` made no pictures | `--limit N` means the first N pictures, not N more | Use `--limit 25` to reach s25 | Note |
| A folder could not be renamed (`Permission denied`) | Windows: something held the folder open | Move its contents instead | Note |
| Stopping a background run | Stopping the wrapper must also end the Python process; two runs on one server collide | Check no `python` run is left; queue runs one after another | Note |
| Flow-era tests broke when Qwen became the default | Tests assumed Flow, the crop and 4 people per picture | `tests/conftest.py` keeps those defaults for the test world | Fixed |
| 27 tests fail on the branch | Work in progress: "Phase" chapter titles, heading removal, `strip-heading` not registered, comic-panel sizes, kcomfy size steps, a render font check | none yet | Open (not caused by the Qwen change) |
