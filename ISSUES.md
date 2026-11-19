# Issues we hit, and what fixed them

A reference for the picture and translation pipeline: each problem we met while moving from Google Flow to Qwen-Image
on Kaggle (November 2026, COI episode 1), what caused it and what we did. **Status**: fixed, partly fixed, or open.
Every fix below is in the code or the bible, so a new episode gets it without doing anything.

## Starting an episode (what episode 1 taught)

1. **Characters first** (once per book, again for new people): `novel ktrial front --chapter 1` makes one front view per
   character (`bible/qwen/front/`); look at each one. A wrong sex or age is a bible problem: fix `look` and `face` in
   `bible/characters.json`, then `--keys name --redo`. These front views are the references of every picture.
2. **Words**: `novel write` (Hindi with the free Gemini keys, then Argon; scene lines with Claude Sonnet 5.5 first).
   Check `novel quota` if Gemini is slow.
3. **Pictures**: `novel images` (two Kaggle notebooks, more if free; each picture lands in `episodes/EXXX/images/cNNNN`
   at once). `--qa` only for an unattended night run (it uses the OpenCode Go allowance).
4. **Look at the pictures**: duplicates, small far faces, a stiff line-up pose, an empty picture with a person in it.
   Redraw one: `novel ktrial run --chapter N --only s41 --reroll` (then `novel images` copies it). Write any new kind
   of problem in this file.
5. **Hook and recap**: full screen with pan and zoom (Ken Burns), never comic panels; hook pictures are reviewed one by
   one (two seeds each to choose from).

## Translation and recap

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Pictures repeated one place; fewer distinct scenes than before | The translation squeezed Lumian's told story into ~13 windows and stretched the tavern talk to ~35. Picture lines follow the windows, so one window held 3 events and the morgue came 6 times in a row. Not the image prompt: the model, seeds and sheets were identical. | Narration rules: open by setting the scene (where, when, who, by name); a story a character tells is retold beat by beat, never one summary line; talk around it stays short. Target 900 words (band 720-1080, about half of a 2,000-word chapter). | Fixed (chapter 1: 68 windows, the story over ~32) |
| A chapter heading line was narrated | The narration started with "चैप्टर एक: ..." | No heading line in the narration (`heading.strip`, `novel strip-heading`) | Fixed |
| Formal / pure Hindi creeping in | | `HINGLISH` rule: common daily-life Hindi, never shuddh (पानी not जल, घर not निवास) | Fixed |
| Every chapter 1 translation needed a 2nd call | The name check demands every English name appear in Hindi; the model drops the minor place "Bigorre" (बिगोर) every time | none yet | Open: relax the check for names the model may leave out |
| Recap too long, or never the right length | 20-25% of an episode is ~2,000 words; one call returned 200-500 words or the whole text; per-chapter calls overshot by 10% | Recap is one call per episode after the translation: 60-90 words (30-40 s), 4-6 short paragraphs, `<episode>/recap.txt`; a model overshoot margin is accepted | Fixed |
| Retranslation refused | Episode marked `published: true` | Set `published: false` in `episode.yaml` before, back to `true` after the re-upload | E001 is unpublished now |
| `novel write --translate 11-20` asked for the recap after every chapter (4 calls each, 40 in all; half a day's free calls) | `run_retranslate` calls `run_translations` once per chapter, and the recap only waits for "every chapter has Hindi", which old Hindi satisfies | `run_translations(recap=False)` in the loop, one `run_recap()` after it (test `test_retranslating_two_chapters_makes_one_recap_at_the_end`). A run started before the fix: its recap fails per chapter on Argon (empty replies); run `novel write --recap` once at the end | Fixed |
| Argon (fallback) wrote 1,462 words for a 900-word chapter | It keeps less to the length than Hydrogen | Requests that reach Argon get "keep the whole narration under 800 Hindi words"; Gemini itself never sees it | Fixed |

## Gemini, Claude and OpenCode (who writes)

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Bad picture lines: a told story became 22 empty morgue pictures; tavern opening lost | gemini-3.5-flash-lite cannot follow the told-story rule | `gemini.api_model: gemini-4-hydrogen` (user settings) | Fixed |
| Free Gemini keys ran out mid-episode; calls failing for minutes | 20 calls a day per key on the free tier; 503s when Hydrogen is busy | Every call counted per key (`gemini_usage.json`, `novel quota`); order **free keys -> Argon on OpenCode Go -> OpenRouter**. `NOVEL_FORCE_OPENROUTER=1` sends everything to OpenRouter | Fixed |
| After a "daily quota gone" the next call was refused (HTTP 400) | Our bug: the 429 handler stored Google's error text in `body`, the request's own name, so the error text was sent next | Renamed; a test sends the same request to the next key | Fixed |
| Scene lines: which model writes them best | Tested on chapter 2 (same rules and checks): Hydrogen 1 try, 38 two-person, 4 warnings; Sonnet 5.5 1 try, 22, 6, the most varied camera; Argon 1, 40, 8 ($0.30 of Go); Qwen4 Max 1, 38, 6 ($0.41); Kimi K3.1 Pro 1, 35, 8 ($1.28); Haiku 5.5 2 tries, 19, 11 | **Claude Sonnet 5.5 first** (headless Claude Code on the logged-in subscription: `claude -p`, effort medium, a fresh unsaved session per chapter, no tools, our rules only), then the free keys, Argon, OpenRouter (`gemini.scene_writer`, `gemini.scene_claude`). Translation never goes to Claude | Fixed |
| The `sonnet` alias of Claude Code answered with Sonnet 4.6 | The CLI's alias | The full name `claude-sonnet-5-5` | Fixed |
| OpenCode free-tier models refuse scripts | "Free models work in OpenCode" (the app only) | Only Go's own free models answer a script: LongCat 2.5, Hy4 flash, Beam (Hy4 flash answers empty) | Note |
| OpenCode Go allowance | Split per model; one model's share used up demotes the whole plan to free | Budget Go calls: a QA check ~$0.005-0.01, an Argon translation ~$0.02, an Argon chapter of scene lines ~$0.30; QA is opt-in (`novel images --qa`) | Rule |

## Picture lines and prompts (what each picture shows, how it is said)

| Problem | Cause | Fix | Status |
|---|---|---|---|
| While Lumian tells a story, the tavern is shown | The scene writer drew the room it is told in | Rule: a told event is drawn itself, in its own group and place, one new moment per picture | Fixed |
| Morgue pictures described as "Lumian's room" | The bible had no morgue | Story places added to the bible; the scene call may add places | Fixed |
| The same face 4-5 pictures in a row | Weak moods ("calm", "focused") | Every expression names mouth, eyes and brows and changes from the person's previous picture | Fixed |
| A person twice: the barman drawn as a second Maurice doing Ryan's gesture (c1 s60) | Maurice's name was in Ryan's line and in the SCENE: each mention pulls in his reference | A name appears only in its own line; elsewhere the person is named by role ("the bartender") | Fixed |
| A person twice: "far left, behind the bar" with the bar drawn on the right (c1 s60), "on the left" in the scene (c3 s31) | With no place picture Qwen lays out the room itself; left/right then clashes with it | No left/right in positions or the scene when there is no place picture; the spot stays ("behind the bar") | Fixed |
| A person not in the picture drawn anyway ("Lumian Lee waits" in a Ryan/Maurice picture, c1 s54) | Their name in the scene | A bible person outside the cast becomes "someone out of view" | Fixed |
| Over-the-shoulder: the same person as the back AND facing the camera (c3 s18, s29; c6 s44) | "one seen from behind" never said who; then only "past X's shoulder" was read, not "over X's shoulder" | A back view only when the scene says "past/over/behind X's shoulder" and X does not also act in it (c9 s05: guessing "the first of the cast" made Lumian the back AND the one taking a magazine); then the prompt names whose back it is, drops that person's pose and face, and says "not drawn a second time facing the camera". Otherwise it is drawn as a plain two-person shot | Fixed. Chapters 1-5, 7, 10 were drawn before it: their over-the-shoulder shots are worth a look |
| One named person drawn twice: a second Lumian standing straight beside the crouching one (c13 s15), and facing the camera beside the one at the window (c14 s45) | The SHOT line said "the people full length" / "the people around them visible behind" for a picture with ONE named person and no extras; the model filled "people" with a copy of the only reference | One named person and no background extras: the shot text is singular ("the person full length", "the place visible behind them") (`kprompts.py`, test `test_one_named_person_and_no_extras_never_get_plural_shot_wording`). Both pictures redrawn with the next seed. Pictures drawn before this (EP1, EP2 up to c13) may hold the same fault: look at single-person wide, medium, low-angle and high-angle shots | Fixed |
| A monster killing itself: a second, standing monster swinging the axe at the one on the ground (c13 s48) | Two causes: the over-the-shoulder template told the model the front person "faces the camera, from the chest up", but that person was lying on the ground; and the action said "he drives the axe" while Lumian was only the back of a head, so the visible monster got the axe | A front person who lies (pose or position says lying, fallen, on the ground, on its back) is told "lies on the ground beyond them, as described above, drawn once" (`kprompts.py`, test `test_an_over_the_shoulder_front_person_lying_down_is_not_told_to_face_the_camera`). The line of c13 s48 was rewritten through `novel import` (a plain high-angle shot, Lumian standing over the monster) and the picture redrawn | Fixed |
| Sentences starting with "The" read "Someone out of view ..." in the prompts ("Someone out of view axe blade flashes in the mist"), which may add stray people | The bible person "The Owl": `_offstage` also replaces the first word of every name not in the cast, so "The" was taken for the owl | The first word is never an alias when it is an article ("The", "A", "An") (`kprompts.py`, test `test_the_first_word_the_of_a_name_is_never_an_alias`) | Fixed. About 110 of the 770 prompts drawn before it (EP1 22, EP2 c11-c15) have the garbled wording: look at their pictures for extra people |
| Two people standing side by side facing the camera, like a line-up (c6 s41) | The front-view references show a straight, front-facing stance and Qwen copied it | Every picture with people: "take only face, hair, body and clothes from the references, never that stance"; with two: "turned toward each other, not posing for the camera" | Fixed |
| Distorted faces of people far from the camera (c6 s41, s42) | "wide" said "people small"; high/low-angle shots were drawn full length and small | Wide, low-angle and high-angle shots: "the people full length, filling the frame from head to feet, faces clear". The user prefers full figures to a crop at the knees (tried and dropped); full-length faces stay smallish but clean (c8 s59 kept). A higher resolution for wide shots (1664x928) is the option if faces ever need to be sharper | Fixed |
| A woman with a lamp in a picture of bare ground (c6 s11) | A "detail" shot with nobody in it and nothing to show; a weak "Nobody in the picture"; the style still asked for "expressive anime eyes" | "Nobody in the picture: no person, no figure, no face and no silhouette anywhere, not even small in the distance"; no anime eyes in empty pictures | Fixed |
| Day or night wrong in the dream ruins | The dream has its own light (novel ch. 6: a white fog-sky, no sun, "as if in the dead of night, under the light of the stars"); the groups only know dawn/day/dusk/night | Dream places carry `fixed_light` in `bible/locations.json` (ruins_wilderness, burned_ruin_building) and every picture there gets it; the scene writers set day or night for every group from the narration | Fixed |

## Characters and references

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Faces alike across characters | One seed for everyone; bible looks name hair, eyes and clothes, not the face | Each character gets a `face` line (Gemini, 8 per call so they differ: sex and age first, face shape, nose, brows, build, age marks) and a seed of their own | Fixed |
| Multi-view sheets made five small copies of a person; duplicates | A sheet shows front, side, back and two close-ups | References are one detailed **front view** per character (`bible/qwen/front/`, 1088x1472); the sheet only when there is no front view | Fixed |
| Tilted heads in the front views | The expression lines (smiles, half-grins) gave an anime "charm tilt" | Front views use an ID-photo pose (head upright, level, eyes on the viewer, calm closed mouth) and leave the expression out | Fixed |
| Martie drawn as a man in a dress | The bible said only "female relative"; the face line had no sex | From the novel (ch. 54): Pierre Berry's mother, early fifties; face lines now start with sex and age | Fixed |
| The death knight's horse merged into his body | The look ends "mounted on a skeletal white horse" | Front views drop "mounted on / riding" and say "alone and on foot" | Fixed |
| An outfit variant had no reference (the owl's "legend") and the prompts of chapters 3, 6, 8, 9 stopped | Variants took a Flow portrait, no longer made | A variant without a portrait uses the person's front view; the words carry the change | Fixed |
| A nameless extra drawn with the main character's face (2nd Lumian pushing the trolley) | The extra had no look | Every extra gets a look of its own (age, build, hair, clothes) | Fixed |
| Five thugs behind Pons drawn as five copies of Pons (c8 s32) | The group was described only as a group ("five thugs in coarse vests"), in Pons's own clothes; his reference was the only face in the prompt | Prompt: the extras line adds "each with a face, age, build, hair and clothes of their own, unlike [the named person] and unlike each other". Writers' rule: a group is described by how its people differ, never in the named person's clothes. Chapters written before the rule may need a line edited by hand (as c8 s32) | Fixed |
| White specks in the eyes | Model habit | "Clean eyes, no white specks" (the negative prompt is ignored at cfg 1) | Fixed |
| Hook pictures: Lumian smiling while chased, standing stiff, a sign with letters | The intro prompt had no pose or face per person and no "no lettering" | `shots.json` gives each person WHERE, POSE and FACE; "never the reference's stance"; "every sign blank" | Fixed |

## Picture QA (`novel images --qa`)

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Which models see duplicates | Tested on labelled chapter 1 pictures: Argon 6/6, Qwen4 Max 5/6, Haiku 4/6, LongCat 2.5 3/4, none failing good ones; GPT-6 Luna and Qwen3.7 Plus failed every good picture; Hy4 and the free OpenRouter vision models (Nemotron 3.5 Super VL, Gemma 4, Ling, Inkling) answered empty, were rate-limited or refused | Panel **LongCat 2.5 (free) + Qwen4 Max**, Haiku as spare, **Argon** settles any fail and lists the fixes for one redraw | Fixed |
| QA cost on every run | It uses the Go allowance | Off by default; `--qa` for unattended runs; a person normally looks at the pictures | Fixed |

## Kaggle, runs and files

| Problem | Cause | Fix | Status |
|---|---|---|---|
| Image server and narration fighting for one account | One account was set by hand for both | `kaggle.pick_free`: each job takes the first of prakash, seema, shashi with nothing running | Fixed |
| Slow: one picture every ~50 s | One server | Two notebooks (more when an account is free), one shared queue on the PC: whichever is free draws the next picture, a dead server's picture goes to another | Fixed |
| The second notebook waited in Kaggle's queue | Kaggle runs one notebook at a time per account | Each notebook on its own account (`images.kaggle_profile_2`, else the next free one) | Fixed |
| A second ComfyUI on the notebook's other T4 never started | Not enough RAM for a second copy of the model | Removed | Fixed |
| The model on one T4 and the text encoder on the other | Tried on shashi: 67.5 s a picture against ~58 s on one card | Kept one card (`images.split_encoder: false`) | Tested, not used |
| Only one image server at a time | A free ngrok account opens one tunnel | A named Cloudflare tunnel per notebook (`images.cf_hostname`, `_2`, `_3`); ngrok only notebook 1's fallback | Fixed |
| A notebook on the ngrok domain answers "403 ERR_NGROK_727" (2026-11-18) | The ngrok account used its free monthly HTTP requests | Cloudflare tunnels only | Open until the month resets |
| New server's tunnel "offline" (ERR_NGROK_3200) on prakash | Cause not found; prakash works on Cloudflare | Notebook 2 on prakash uses Cloudflare | Partly fixed |
| Pictures went to a work folder, not the episode | They were copied at the end of a chapter | Each picture is copied into `episodes/EXXX/images/cNNNN` as soon as it is drawn | Fixed |
| The server was gone after a pause | It stops itself after 20 idle minutes; a restart takes 5-10 minutes | `novel images` restarts it by itself | Note |
| `--limit 10` made no pictures | `--limit N` means the first N pictures, not N more | Use `--limit 25` to reach s25 | Note |
| A folder could not be renamed (`Permission denied`) | Windows: something held the folder open | Move its contents instead | Note |
| Flow-era tests broke when Qwen became the default | Tests assumed Flow, the crop and 4 people per picture | `tests/conftest.py` keeps those defaults for the test world | Fixed |
| Tests could call real services | Keys are in secrets.env | `conftest.py` turns off the Gemini count, the OpenCode fallback and headless Claude in tests | Fixed |
| 27 tests fail on the branch | Work in progress: "Phase" chapter titles, heading removal, `strip-heading` not registered, comic-panel sizes, kcomfy size steps, a render font check | none yet | Open (not caused by the Qwen change) |
