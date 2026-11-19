# COMMANDS: the run sheet

Everything you type is `novel <command>`. Open a terminal (Windows Terminal or PowerShell) and type it there.

- Not able to type `novel` by itself? Use the full path: `"D:\Novel Pipeline\bin\novel.cmd" status`. Or run `novel doctor --fix-path` once and open a new terminal.
- Every command can show its own help: `novel images --help`.
- Boxes marked `cmd` are commands to copy. Boxes marked `text` are what the screen shows.
- The examples use the book **coi** (Circle of Inevitability), episode 3 = chapters 21-30.

**Two ways to write (read this first):** `novel write` lets the Gemini API (your keys) write the Hindi of a new chapter, the bible and the picture lines, and files them. The older way, pasting a prompt into the Gemini app and filing each reply with `novel import`, still works and ends in the same files. The by-hand "full" mode (Gemini writes the Hindi in the app) is **not finished**: `novel import` recognises the Hindi block and keeps it in `inbox/`, but does not turn it into the locked Hindi file. For a chapter without Hindi use `novel write`. Steps that need the unfinished by-hand part are marked **[FULL MODE: NOT BUILT]**.

---

## Every day

Use this when an episode is already started and you are only waiting for pictures. Three commands.

**1. See what is left.**

```cmd
novel status
```

You see: a line like `[coi · E003 · ch 21-30]`, a table with one row per chapter (columns: text, hindi, lock, qa, scenes, chars, images, audio, capt, video), then `VERDICT: NOT READY (1 blocker)` or `READY TO BUILD`, then `Missing, in the order to run it:` with a numbered list. Every line of that list ends with the command that fixes it. **The first line is your next action.**

**2. Make today's pictures.** Pictures are drawn by **Qwen-Image on Kaggle** (setting `images.engine: qwen`). No Chrome, no Flow.

```cmd
novel images
```

You see, per chapter, `chapter 2: 68 pictures to make`. If no image server is running it starts **two** notebooks, each on its own free Kaggle account (`Kaggle account seema: free`, then `Kaggle account shashi: free` for the second, then `still starting, 2 min` ... `the server answers at https://coi-pipeline...`; about 5-10 minutes; the second joins by itself when it answers). Then one line per picture, like `s12  49.3s  avg 51.1s  3 left, ~3 min`, with `--qa` also a QA line per picture (`QA s12: pass`, or `QA s12: failed (duplicate_person); redrawing with the fixes` then `QA s12: fixed after the redraw`) and the QA summary (`picture QA: 64 pass, 2 kept, 1 fixed, 1 failed; 2 redrawn; NEEDS A LOOK: s41`), and `chapter 2: 68 pictures copied into ...\images\c0002`. Each person is drawn from their front view (`bible\qwen\front\`); a new character without one gets a character sheet first.

How much it does: every chapter of the episode that has picture lines and is missing pictures. About 50 seconds per picture per server: a 68-picture chapter takes about an hour on one server, about half that on two. `--chapters 2-3` picks chapters, `--max 25` stops each chapter after its first 25 pictures, `--qa` adds the picture QA (each picture checked and a failed one redrawn once; it uses OpenCode Go allowance): leave it off when you look at the pictures yourself, use it for a night run. Ctrl+C is safe; run it again and finished pictures are skipped. The servers stop by themselves after 20 minutes without work.

Pictures that QA marked `failed` (or `draw_failed`) are listed at the end; `novel ktrial qa --chapter 2 --status` shows every picture's state, tries and what QA found. Look at those few by eye (`novel sheet --chapter 2 --open`).

(Flow instead: set `images.engine: flow`; then Chrome must be open and signed in to Flow, see `novel chrome`.)

**3. See what is left again.**

```cmd
novel status
```

You see the same table. The `images` column has grown. When the first line of the Missing list is no longer about pictures, you are done for the day. When the verdict is `READY TO BUILD`, go to step 14 below.

---

## One episode, start to finish

Each step: the command, what you do outside the terminal, what you should see, how long. Example: COI episode 3, chapters 21-30 (the Hindi is already locked: scenes mode).

Times: only numbers that the tool itself gives are stated. Everything else says "varies".

### Step 1. Pick the book and episode

```cmd
novel use coi --ep 3
```

You see: `[coi · E003 · ch 21-30]`. From now on every command works on that episode; you do not repeat `--book` and `--ep`.

**A brand-new episode** (not COI 3-6, which already exist) is made from the extracted chapters instead:

```cmd
novel episode add
```

You see: `Created episode E007: chapters 61-70 (10 files) in ...` and `Next step: novel prompt --chapters 61-70 --copy`. It makes the next free episode and picks it. If you want a shorter or specific range: `novel episode add --chapters 61-65`. To see all episodes: `novel episode list`.

Takes seconds.

### Step 2. Ask what is missing

```cmd
novel status
```

For E003 you see, in the Missing list: `chapters 21-30: no scenes yet -> novel write   (or by hand: novel prompt --chapters 21-30 --copy, ...)`. That is step 3.

### Step 3. Write the bible and the picture lines with the Gemini API

```cmd
novel write
```

It takes the next 5 chapters of the episode that need work (setting `gemini.chapters_per_run`; to choose them: `novel write --chapters 21-25`). A chapter with no Hindi is translated first and locked; a chapter with Hindi keeps it untouched. Then it asks Gemini for the bible of those chapters and for the pictures of each chapter, files every answer exactly like `novel import` does, and asks again when an answer is rejected. You see one line per step, then a table with one row per chapter, the path of the run log and `Next: novel qa --chapters 21-25`. Run the same command again for the next 5 chapters, or to finish a chapter that stopped: finished chapters are skipped.

Want to see the plan first? `novel write --dry-run` shows what it would do and how big each request is, and sends nothing. More in "Writing with the Gemini API" below.

If you prefer the Gemini app, or `novel write` keeps failing on a chapter, do steps 3 (by hand) to 6 below instead; skip them when `novel write` worked.

### Step 3 (by hand). Make the Gemini prompt

```cmd
novel prompt --chapters 21-30 --copy
```

You see: `Batch B021-030 (scenes mode) for book 'coi', chapters 21-30`, a list of **files to upload** (full paths), `Prompt: ...prompt.txt`, and for scenes mode the number of pictures per chapter (for COI 21-30: 571 pictures in total). The prompt is now on your clipboard (whatever you had copied before is gone).

For new chapters (no locked Hindi) the same command makes a "full" batch. **[FULL MODE: NOT BUILT]** (see the note at the top). Use `novel write` for those.

If the clipboard is not available it says so; open the `prompt.txt` it names and copy the text by hand.

Takes seconds. Do not use `--out` for real work: `novel import` looks for the batch in the book's `gemini/` folder and will not find it elsewhere.

### Step 4 (by hand). Start the Gemini conversation

Outside the terminal. Details are in "The Gemini conversation" below. In short:

1. Open https://gemini.google.com/ , model **Argon High**, inside your notebook **Audiobook Manga/Anime**. Start a **new chat**.
2. Upload the files that step 3 listed (`chapters-21-30.txt`, `bible.txt`, and in scenes mode `windows-21-30.txt`).
3. Paste the prompt (Ctrl+V) and send.
4. Gemini answers with one block called BIBLE. Click the copy button under its reply.

Takes as long as Gemini takes: varies.

### Step 5 (by hand). File the reply

```cmd
novel import
```

It reads the clipboard. You see a line per block, for example `BIBLE B021-030  ok: ... applied to the bible`, a **Batch progress** table, and the last line: `Say to Gemini: next`. That last line is the exact words to type into Gemini next. If it says `REJECTED`, see "The Gemini conversation".

Want to check without saving anything? `novel import --dry-run`.

### Step 6 (by hand). One chapter at a time

Repeat until the batch is complete (for a 10-chapter episode: 10 rounds):

1. In Gemini type what `Say to Gemini:` told you (normally `next`).
2. Click the copy button under the reply.
3. In the terminal: `novel import`

```cmd
novel import
```

You see: `IMAGES c0021  ok: ...` and, when that chapter has all its pictures, `chapter 21 complete: 65 images in N groups -> ...`. When the whole batch is done: `Say to Gemini: nothing: batch B021-030 is complete. Next: novel qa --chapters 21-30`.

Rough size: COI chapter 21 has 65 pictures, chapter 23 has 87. A long chapter may arrive in two parts (see `>>> MORE`).

### Step 7. Check the work (QA)

```cmd
novel qa --chapters 21-30
```

It runs checks in code, then asks an AI reviewer (setting `qa.reviewer`: `claude`, with `gemini` as the fallback) to read each chapter. You see a table (`Chapter | Pictures | Code checks | AI review | Overall`), a line `Report: ...qa\report.md`, and `Next: novel approve --chapters 21-30`. A chapter that says `FIX` must be corrected first: see "When something stops".

To use no AI at all: `novel qa --chapters 21-30 --only-code`. A chapter that was never reviewed by an AI is still approved, and the tool says so.

Takes: varies (one AI call per chapter unless `--only-code`).

### Step 8. Approve (Flow only)

With Qwen (the default) skip steps 8 and 9: `novel images` builds its own prompts from the picture lines. They are for `images.engine: flow`.

```cmd
novel approve --chapters 21-30
```

You see, per chapter: `chapter 21: 65 scenes, 65 prompts written, 0 unchanged, 0 kept, 65 pictures still to generate.`, then `flow/plan.json: 571 shots in the plan`, and `Next: ...`. Approve locks the Hindi if it is not locked yet, writes one Flow prompt per picture and the plan the image step reads. Want a rehearsal first? `novel approve --chapters 21-30 --dry-run` writes nothing.

If a chapter is `REFUSED`, the message says what is wrong and how to fix it. `images.flow_project` must be set (COI has it).

If it prints **Flow Characters to create before generating** with a `novel characters make --keys ...` line, do step 9. If it does not, skip step 9.

### Step 9. Create missing Flow Characters (only if step 8 listed some)

Open Chrome first (step 10a). Copy the `novel characters make --keys ...` line that step 8 printed and run it, for example:

```cmd
novel characters make --keys tarot_lady
```

It shows what it will create and asks you to confirm (`--yes` skips the question; `--dry-run` only lists). Each character uses Flow credits for a portrait and a body picture. You see `made: tarot_lady 'Tarot Lady v2'` and `made 1, not made 0`. To look at the cast first: `novel characters list`; to see references that carry a held item: `novel characters props`.

Takes: varies (Flow).

### Step 10. Narration, captions and pictures

**10a. Open Chrome** (Flow only; skip with Qwen, the default):

```cmd
novel chrome
```

You see `Chrome answers on port 9222 (...)`, then three things to do in that Chrome window: sign in to Flow if asked, open the book's Flow project, and in the prompt bar choose **one 16:9 image** with the model in `images.flow_model` (Nano Banana Pro). Leave this window open and visible. `novel` never closes it.

**10b. Start everything:**

```cmd
novel run
```

It starts the narration (Kaggle) and the captions as **background jobs** and then makes pictures in **this window**, exactly like `novel images`. You see `tts       started   pid ...` and `captions  started   pid ...` and then the picture progress lines. The narration and the image server run on two different Kaggle accounts (see "Pictures with Qwen-Image on Kaggle").

- The narration job pushes the Hindi to Kaggle, waits (it checks every `tts.poll_minutes` = 10 minutes) and brings the audio home. Time on Kaggle: varies. If the audio already exists (COI 3-6 do) it says `nothing to narrate`.
- Captions are the English subtitles, written by Gemini through your API keys. They need no work from you.
- See the background jobs with `novel jobs`; read a log with `novel jobs --tail tts` (add `--follow` to keep watching).
- Only want pictures? Use `novel images` (the daily command) instead of `novel run`.
- Ctrl+C once stops after the current picture. Run the same command again to continue; finished pictures are never sent twice.

### Step 11. The following days: more pictures

```cmd
novel images
```

Run it until `novel status` shows all pictures done. With Qwen one run does every chapter that has picture lines (about an hour per 68-picture chapter); stop with Ctrl+C and run again whenever you like. To choose chapters yourself: `novel images --chapters 24-25`. With Flow it takes 3 chapters and at most 200 pictures per run; after a Flow stop, see "When something stops".

Then make the 1920x1080 copies (or let `novel build` do it): `novel hd`.

### Step 12. Look at the pictures (optional)

```cmd
novel sheet --chapter 21 --open
```

It writes a web page with every picture of the chapter in order next to its text and opens it. Use it when `novel status` says a picture needs a look, or just to check your work.

### Step 13. Subscribe cards (once per book, then only when you change their words)

Skip this if `novel status` does not mention subscribe cards. Without cards the finished video simply has no subscribe card.

```cmd
novel extras voice
```

It writes the spoken message and prints the next three commands. Run them as printed (narration of the message goes to Kaggle like a chapter does):

```cmd
novel tts push --ep 900 --chapters 9001
novel tts pull --ep 900
novel extras cards
```

Wait for the push to finish on Kaggle before `pull` (`novel tts status --ep 900` shows how far it is). You see the card videos being built. COI already has its cards. They go stale when you change the card words, the logo or the fonts; run `novel extras cards` again then.

### Step 13b. Intro hook (optional; before the first chapter)

A short spoken hook (or a recap of the last episode) that opens the video: full-screen pictures with pan and zoom (Ken
Burns, never comic panels), drawn with Qwen-Image-2.1 on Kaggle (not Flow) from the characters' front views. It lives in `extras\E9NNN` (9000 + the episode number). Put the Hindi in a text file, one paragraph
per picture (about 5-7 s each), and `novel intro shots` writes `extras\E9NNN\shots.json` with one shot per paragraph (or write it by hand)
(`{"id": 1, "cast": ["lumian_lee", "skinless_monster"], "shot": "...", "scene": "..."}`; `cast` = bible keys whose approved front
views in `bible\qwenront\` are sent as references).

```cmd
novel intro init --ep 1 --text-file hook.txt
novel intro shots --ep 1                                  (Claude writes shots.json from the paragraphs; --redo to write it again)
novel tts push --ep 1 --account seema     (narrates the chapters AND this intro/recap in one Kaggle run)
novel tts pull --ep 1
novel ktrial up --chapter 24 --model qwen-image-turbo     (only if the Kaggle ComfyUI is not running)
novel intro images --ep 1                                 (--only s02 --seed 303 for another try)
novel intro render --ep 1
novel build
```

The join puts the intro first and lists it as `0:00 Intro`; chapters are listed as `Phase N: <title>`.

### Step 14. Build the video

When `novel status` says `READY TO BUILD`:

```cmd
novel build
```

It renders every chapter that is not current, joins them with the intro hook when there is one (and the subscribe cards; the disclaimer only when `video.disclaimer.enabled`) into one file and writes YouTube timestamps. You see per chapter `rendered (...), pass, 612.4 s, 410.2 MB`, then `video: ...\E003.mp4`, `duration: ...` and `timestamps:` lines. Then `next: novel meta`.

- Not ready yet? It lists what is missing and stops before doing any work. To wait for the rest and then render slowly (default 2 chapters an hour): `novel build --when-ready` (change the pace with `--per-hour 3`). Ctrl+C is safe; run it again.
- A quick look at the start of one chapter: `novel render --chapter 21 --end 30` renders the first 30 seconds of chapter 21.
- Render time: varies.
- A chapter that says `FAIL`: see "When something stops".

### Step 15. Title, description and tags

```cmd
novel meta
```

It writes `publish\publish.txt` and `publish\publish.json` in the episode folder: title, description with the real chapter timestamps, tags, hashtags and three thumbnail lines. It uses research (search suggestions, needs `YOUTUBE_API_KEY`; skipped with a warning if missing) and an AI (`publish.ai`, default `claude`, fallback `gemini`). You see `Title: ...`, the length counts, and `valid. Next: paste publish.txt into Studio, then: novel thumb`.

To write it with no AI: `novel meta --ai none --teaser "Do ya teen line Hinglish mein, kahani ka teaser"`. Then you pass the thumbnail lines yourself in step 16.

### Step 16. Thumbnails

```cmd
novel thumb --image lumian_lee
```

`--image` is a character key (`novel characters list` shows them) or a path to a picture. It makes `thumb-a.jpg`, `thumb-b.jpg`, `thumb-c.jpg` and `sheet.jpg` in the episode's `publish` folder. The three text lines come from `publish.json`. Without `novel meta`'s AI, give them yourself, one `--hook` for each thumbnail (at most 3):

```cmd
novel thumb --image lumian_lee --hook "KYA LUMIAN|BACH PAYEGA?" --hook "EK *RAAZ*|SAAMNE AAYA"
```

Format of a line: `LINE ONE|LINE TWO`, with `*...*` around the keyword that gets the colour. Tune the face with `--focus 0.53,0.40`, `--zoom 1.5`, `--face left`.

### Step 17. Upload (outside the terminal)

In YouTube Studio: upload the episode video (the path `novel build` printed), paste the title, description and tags from `publish.txt`, set the thumbnail (try all three with Studio's "Test & compare"), schedule or publish.

### Step 18. Mark it done

```cmd
novel published --url https://youtu.be/AbCdEfGhIjK
```

You see `marked as published: ...` and `Next episode: novel episode add --book coi`. `novel status` now says `PUBLISHED` for that episode. Start the next one at step 1.

---

## Writing with the Gemini API

`novel write` does by code what the Gemini conversation below does by hand. It uses your API keys (`GEMINI_API_KEY`, `GEMINI_API_KEY2`, ... in `secrets.env`) and the model in `gemini.api_model` (default Gemini 3.5 Flash Lite; this PC's settings use `gemini-4-hydrogen`). Calls stay inside the free tier per key and go to OpenRouter only when every key has used its day (see "Free tier and OpenRouter" below).

**What one run does** (for the next 5 chapters that need work):

1. A chapter with **no Hindi**: one call writes the Hinglish narration. It is checked (no digits, no Latin letters, no notes, the heading line, the names of the NAMES list, the length against the English), stored in `hindi/` and locked. A bad answer is asked again with the problems listed. A long chapter that is cut off by the length limit is continued from its last full paragraph. A chapter that **already has Hindi is never translated again** (its narration and timing stay as they are).
2. The **bible** of the chapters: one call.
3. The **pictures** of each chapter: one call per chapter. Each call carries the rules, the bible as it is at that moment (so people and places added by an earlier chapter of the run are known), that chapter's English text and its windows only.
4. Every answer goes through the same importer as `novel import` (raw copy in `gemini\<batch>\raw\`, checks, progress, the scene file). When it says an answer is wrong, the tool asks again with the FIX list and the rejected block; when it says the block stops early, it asks for the missing windows only. Each block is asked at most 3 times (`gemini.max_attempts_per_block`); a block that still fails is left as it is, reported, and the run goes on with the next chapter.

**Who writes what.** The translation (and the recap): the free Gemini keys, then Gemini 4 Argon on OpenCode Go (told to stay under 800 Hindi words), then OpenRouter. The scene lines: **Claude Sonnet 5.5** first, through headless Claude Code (`claude -p`) on the Claude subscription logged in on this PC (a fresh, unsaved session per chapter; effort medium), then the free Gemini keys, Argon and OpenRouter (`gemini.scene_writer`, `gemini.scene_claude`, `gemini.opencode_fallback` in docs/settings.md). If Claude stops (its usage limit, for example), the run goes on with the next writer.

**Free tier and OpenRouter.** Every Gemini call (write, recap, faces, captions, meta) is counted per key in `<novel home>\gemini_usage.json`: at most 5 calls a minute and 20 a day per key (`gemini.free_tier`; the day resets at midnight Pacific, as Google's does). Before each call the tool takes a key with calls left (`key 1 of 4`, never the key); when every key used its minute it waits (`free tier: every key used its 5 calls this minute, waiting 40s`); when every key used its day the call goes to **OpenRouter** (`OPENROUTER_API_KEY`, the same model as `google/<model>`, billed per token). A 429 from Google holds that key a minute, or for the rest of the day when Google says the daily quota is gone. `novel quota` shows the counts. To send everything to OpenRouter (Gemini overloaded, the work cannot wait): `set NOVEL_FORCE_OPENROUTER=1` for one command, or `gemini.force_openrouter: true`. It also waits `gemini.min_seconds_between_calls` (4) between calls. Which model wrote each block is kept in `progress.json` and in the scene file (`source.model`).

**Options.**

- `novel write --dry-run`: the plan and the size of each request; sends and writes nothing.
- `novel write --chapters 21-25`: choose the chapters. `--max-chapters 3` takes fewer (or more) than 5.
- `novel write --redo 23`: write the scenes of chapter 23 again. The old files move to `old\v1\`; nothing is deleted.
- `novel write --fix 23`: after `novel qa` said FIX for chapter 23, send the same correction message that `novel qa --fix-prompt 23` prints to the API and file the PATCH it returns. Then run `novel qa --chapters 23` again.
- `novel write --no-translate`: refuse a chapter that has no Hindi instead of translating it.
- `novel write --parallel 2`: ask 2 chapters at the same time (setting `gemini.parallel`, default 1). Answers are still filed one at a time. If two chapters add the same new place at once, the second one is rejected by the importer (the place exists) and that chapter's pictures are checked against the bible as it then is; if that fails it is asked again.
- `Ctrl+C` stops after the current call and leaves everything consistent. Run the same command to continue.

**The run log** is `gemini\<batch>\run-<time>.log` (every request's size and every answer's verdict, never a key). The requests and answers themselves are in `gemini\<batch>\api\`. The exit code is 0 when every chapter of the run is complete, 1 otherwise.

**Who and where in the frame:** each group lists every named character in the place in `present`; each picture's `cast` is the people visibly in it (fewer is better: the ones the moment is about; a picture where nobody is visible has an empty cast). Everyone in a cast must be in the group's `present` (otherwise the block is rejected) and has a `position` (for example `left foreground, by the door`; two people never share one). Flow gets it as `- <Name>: <position>; <pose>; hands: ...`. `novel qa` warns about a person listed in `present` who is drawn in none of the group's pictures, and about a person named in a picture's text but missing from its cast.

**Where and when** (the `where` and `time` of each group): every group of pictures says `where` (`indoor` or `outdoor`) and `time` (`dawn`, `day`, `dusk` or `night`). Gemini decides them from the narration of that moment, not from the place's kind. They are passed on to Flow as a `SETTING:` line, for example `SETTING: indoors, at night. No daylight through windows; the only light is: ...`. `novel qa` warns when they contradict the light (sunlight at night, moonlight indoors with no window, lamps as the only light outdoors by day), when `where` contradicts the place's kind, and notes a move through the door.

---

## The Gemini conversation

This is the part you do by hand in the browser when you do not use `novel write`. The tool makes the prompt, files what comes back and checks it.

### What to upload and paste

- Open https://gemini.google.com/ , model **Argon High**, inside the notebook **Audiobook Manga/Anime**. Use a **new chat for every batch** (a batch = the chapters you gave to `novel prompt`).
- **Upload** exactly the files `novel prompt` listed under "Upload these files to Gemini":
  - `chapters-A-B.txt`: the English chapters
  - `bible.txt`: names, characters and places decided so far (new characters get added from batch to batch)
  - `windows-A-B.txt`: scenes mode only: the locked Hindi cut into numbered windows, one picture per window
- Do **not** upload `prompt.txt` or `batch.json`.
- **Paste** the prompt (it is on your clipboard after `--copy`; the file is `prompt.txt` in `D:\Novel Data\<book>\gemini\B021-030\`).

### What Gemini answers, and what you type back

| Turn | Gemini sends | You type | Then in the terminal |
| --- | --- | --- | --- |
| first | one `BIBLE` block (new characters, places, names) | nothing yet: copy the reply | `novel import` |
| each chapter | scenes mode: the `IMAGES` block of the next chapter. Full mode: its `HINDI` block, then its `IMAGES` block | `next` | `novel import` |
| reply too long | the block so far and a line `>>> MORE c0021 IMAGES` | `next` (it sends the rest of that block) | `novel import` |
| a block was cut off, damaged or rejected | | `repeat c0021 IMAGES` (or `repeat BIBLE B021-030`) | `novel import` |

**Never guess the words.** After every `novel import`, the last line is `Say to Gemini: ...`. Type exactly that. Examples:

```text
Say to Gemini: next
Say to Gemini: repeat c0021 IMAGES
Say to Gemini: repeat c0021 IMAGES. FIX: image 7: window 99 does not exist: chapter 21 has 65 (n runs 1-65)
Say to Gemini: nothing: batch B021-030 is complete. Next: novel qa --chapters 21-30
Say to Gemini: nothing yet. First run: novel prompt --chapters 21-30
```

If the line has `FIX: ...` after it, type the whole line including the FIX list: Gemini needs the list to know what to change.

### What `>>> MORE` means

Gemini's reply was too long, so it stopped after a complete part of a block and wrote `>>> MORE c0021 IMAGES` on its own line. The tool saves the part and the table shows `partial (Gemini owes the rest)`. Type `next`, copy the new reply, run `novel import` again. The parts are joined; when every picture of the chapter is there, the chapter is `done`.

### How to copy a reply

Click the **copy button under Gemini's reply** (it copies the whole reply, not what you selected). Then run `novel import`: it reads the clipboard. Do not select text by hand; a missing line makes the block unreadable.

If the clipboard fails, or you want to keep the reply:

- save it as a text file and run `novel import --file reply.txt` (use the real path);
- or save each block by hand as a file in `D:\Novel Data\<book>\inbox\` with the name in its BEGIN line (`c0021.images.jsonl`, `bible-B021-030.jsonl`) and run `novel import --inbox`.

Every reply is also kept untouched in `gemini\<batch>\raw\`, so nothing is lost.

### Common rejections

A rejected block writes nothing; the tool prints what is wrong and what to say to Gemini. Warnings (`warning: ...`) do not stop anything.

| You see | It means | Do |
| --- | --- | --- |
| `cut off: no END line (the reply stopped inside the block after N lines)` | Gemini stopped in the middle of a block | Type what the `Say to Gemini:` line says (`repeat c0021 IMAGES`) |
| `no blocks found in the clipboard (N lines)... Copy the whole reply and try again.` | You copied something else (or part of the reply) | Click Gemini's copy button, run `novel import` again |
| `the clipboard is empty. Copy Gemini's whole reply (its copy button)...` | Nothing was copied | Click Gemini's copy button, run `novel import` again |
| `no prompt batch B021-030 (no ...batch.json). Fix: novel prompt --chapters 21-30` | You never ran `novel prompt` for this range, or used `--out` | Run the `Fix:` command, then start a new Gemini chat with the new files |
| `chapter 23 is in no prompt batch...` | Same as the row above | Run the `Fix:` command |
| `not valid JSON at column N: ...` or `not a JSON object line (every line must start with {)` | Gemini wrote a damaged line | Type what the `Say to Gemini:` line says |
| `window 99 does not exist: chapter 21 has 65 (n runs 1-65)` | Gemini numbered a picture that does not exist | Type what the `Say to Gemini:` line says, FIX list included |
| `too few images: windows 60-65 have no image line (59 of 65 received) and the reply has no '>>> MORE c0021 IMAGES' line` | Gemini left pictures out | Type what the `Say to Gemini:` line says |
| `location 'x' is not in the location registry (did you mean y?); add it with a BIBLE location line first` | A picture uses a place nobody defined | Type what the `Say to Gemini:` line says (it asks for the missing place) |
| `unknown character 'x' ... new people need a BIBLE character line first` | A picture uses a person nobody defined | Same: type what the `Say to Gemini:` line says |
| `where: missing ...` or `where: 'inside' is not one of indoor|outdoor` | A group line has no (or a wrong) `where` | Same: type what the `Say to Gemini:` line says |
| `position: missing or empty ...` or `share the position 'centre'` | A person in a picture has no `position` (where in the frame and the place), or two people in one picture have the same one | Same: type what the `Say to Gemini:` line says |
| `tobias_reed is in the cast but not in group g01's "present"` | A person is drawn who the group line does not list in `present` (the group must name everyone involved first) | Same: type what the `Say to Gemini:` line says |
| `hands: missing or empty: say what the hands do...` | A person in a picture has no `hands` | Same: type what the `Say to Gemini:` line says |
| `action: 41 characters; needs 60-600` | The picture description is too short or too long | Same: type what the `Say to Gemini:` line says |
| `uses the banned word '...'` or `must be English (Devanagari found)` | A picture description breaks the content rules | Same: type what the `Say to Gemini:` line says |
| `cast: 5 attached characters (with a Flow reference); at most 4` | Too many named people in one picture | Same: type what the `Say to Gemini:` line says |
| `chapter 21 was already imported (...). To replace them run: novel import --replace` | That chapter is done already | If you really want the new version: run the command it prints. The old files move to `old/vN/`; nothing is deleted. To change only a few pictures, ask Gemini for a PATCH instead (see `novel qa --fix-prompt`) |
| `the Hindi of chapter 21 changed after novel prompt made batch ...` | The Hindi file changed after the prompt was made, so the picture numbers may have moved | Run the `Fix:` command and start a new Gemini chat |
| `revise is not allowed ... run with --allow-revise` | Gemini rewrote an old character description when this batch did not ask for it | If you trust it: `novel import --allow-revise` |

Gemini keeps getting the same line wrong? Run `novel qa --fix-prompt 21` after the chapter was imported: it prints a message for the **same** conversation that asks for a corrected block (add `--copy` to put it on the clipboard).

---

## When something stops

### `novel write`: a rate limit, a quota, or a block that keeps being rejected

**You see** lines like `rate limit: key 1 on gemini-4-hydrogen (held for a minute)` or `free tier: every key used its 5 calls this minute, waiting 40s`: nothing to do, another key is used or it waits by itself. `every Gemini key has used its free calls for today; asking OpenRouter`: the rest of the day's calls go to OpenRouter (paid per token). Many `HTTP Error 503` lines: Gemini is overloaded; after a few drops a call goes to OpenRouter by itself.

**You see** `every Gemini key has used its free calls for today ... and OpenRouter did not answer` (or, with the free-tier count off, `STOPPED: the Gemini quota is used up`). The run ends cleanly with the table of what was finished.

**Do:**
1. `novel quota` shows what each key has used. Try again later with the same command: `novel write`. Finished chapters are skipped, a block that was filed stays filed.
2. Or check `OPENROUTER_API_KEY` in `secrets.env`, add another key (`GEMINI_API_KEY2`), or name another model in `gemini.fallback_models`.
3. Gemini keeps failing and the work cannot wait: `set NOVEL_FORCE_OPENROUTER=1` then the same command (every call goes to OpenRouter).

**You see** `chapter 21: scenes: not complete after 3 attempts; reported, going on`, and `failed` or `partial` in the table. The answer kept being rejected. The lines above it say why (`rejected (2 errors): ...`); the tool already sent those errors back to Gemini each time.

**Do:**
1. Run `novel write` again (it asks again for that chapter only, in the same batch).
2. Still failing? Read the run log (the path is printed) and the last request and answer in the `api` folder next to it. Then use the by-hand way for that chapter: `novel prompt --chapters 21-25 --copy`, paste into the Gemini app, then `novel import` (see "The Gemini conversation").
3. A chapter that is filed but you want written again: `novel write --redo 21`.

**You see** `refused (--no-translate)` or `the Hindi changed after it was locked`: the line tells you what to restore or which option to remove.

### Flow stops the run (CAPTCHA, "unusual activity", sign-in, limit)

**You see** (in the terminal, boxed in `=` lines):

```text
FLOW STOPPED at 14:32: <Flow's own words>
Solve it in the Chrome window.
When Flow works again, press Enter to continue. Type s then Enter to stop here:
```

The tool does **not** retry by itself and does not work around it. It beeps when it stops and beeps again every 30 seconds (`images.alert_repeat_seconds`; 0 = once) until you press a key. The beep only reminds you: the run never goes on by itself.

**Do:**
1. Go to the Chrome window. Clear the CAPTCHA, sign in again, or read what Flow says.
2. Back in the terminal press **Enter** to continue, or type `s` and Enter to stop.

If the message says it is a **usage limit** (`This is a usage limit, not something you can clear in the browser`): type `s`. Pressing Enter only runs into it again. Try again after the limit resets.

**After a stop in a background run** (`novel run --images-in-background`, or nobody at the keyboard): the run just ends. The next `novel images` refuses for `images.wall_cooldown_hours` (12 hours) with: `Flow stopped at ...: ... Open Flow yourself and check the account. When it is clear, run again with --i-checked-flow`. Look at Flow, then:

```cmd
novel images --i-checked-flow
```

It records that you checked and goes on. Same for `novel characters make` (it has `--i-checked-flow` too).

To stop a run on purpose: Ctrl+C once (after the current picture), or from another terminal `novel jobs --stop images`.

### A picture "needs a manual look" (NEEDS CHECK)

**You see:** `NEEDS CHECK (it may already have been sent; never resent automatically): c0021-s05` (and `novel status` says `1 image need a manual look in Flow`). The tool sent the prompt but could not find the picture afterwards, and will never send it twice by itself.

**Do:**
1. Open the Flow project in Chrome and look for that picture.
2. If it is **there**: tell the tool to take it. Run the line the tool printed under `NEEDS CHECK`: `node "D:\Novel Pipeline\flow\adopt.mjs" <path to plan.json> c0021-s05` (the full line is on your screen). Then `novel images` downloads it.
3. If it is **not there**: send it again on purpose:

```cmd
novel images --resend c0021-s05
```

### Flow refused a picture (REFUSED)

**You see:** `REFUSED (Flow would not draw these; the scene text must change): c0021-s12`. The description must be reworded.

**Do**, in this order:
1. `novel qa --fix-prompt 21 --copy` (message for Gemini; the number is the chapter)
2. Paste it into the **same** Gemini conversation. Copy the reply.
3. `novel import` (it files the corrected picture lines as a PATCH)
4. `novel approve --chapters 21` (rewrites the prompt; a picture that already exists is kept)
5. `novel images --resend c0021-s12`

```cmd
novel qa --fix-prompt 21 --copy
```

### QA says FIX

**You see:** in `novel qa`: `Chapter 21: FIX (2 code errors, AI: 1 error(s)). Fix: novel qa --fix-prompt 21`. Also in `novel status`: `QA says FIX (2 errors)`. `novel approve` refuses a chapter in this state (unless you add `--force`, which is recorded; do not do that to save time).

**Do:**
1. `novel qa --fix-prompt 21 --copy` prints (and copies) a message listing the errors.
2. Paste it into the **same** Gemini conversation; copy the reply.
3. `novel import`
4. `novel qa --chapters 21` again.

Only errors are listed. Add `--with-warnings` to include warnings too.

### Narration failed on Kaggle

**You see:** `novel tts watch` (or `novel jobs --tail tts`) ends with `kernel ended with ERROR. Last 15 log lines:` and the lines; or `novel status` shows `narration is not usable (...)`.

**Do:**
1. Read the lines. Also `novel tts status` for Kaggle's own last words.
2. If the run is only still going (`RUNNING`), wait: `novel tts watch`.
3. When Kaggle says it finished (`COMPLETE`) but chapters were not taken, run `novel tts pull`; it names each chapter with `NOT TAKEN: <reason>`.
4. If it ended with `ERROR`: `novel tts reset`, then `novel tts push --chapters 21-23` (see below).

**The run ended with ERROR (or was cancelled):** there is nothing to pull, but `novel tts pull` says `not finished` and `novel tts push` says `...has not been pulled`. Clear the failed run with one command, then narrate again:

```cmd
novel tts reset
novel tts push --chapters 21-23
```

`novel tts reset` shows the recorded run (when it was pushed, which chapters, what Kaggle says now) and asks `[y/N]` (add `--yes` to skip the question). It **refuses** when Kaggle says the run is still going (use `novel tts watch`) or has finished with output waiting (use `novel tts pull`). Audio you already brought home is never touched; the failed run is only marked abandoned in `tts-state.json` and kept in its `history`. `novel tts watch`, `novel tts status` and `novel status` name this command when the run ended in error.

### A render fails its check

**You see:** in `novel build` or `novel render`: `chapter 21: rendered (...), FAIL, ... jerk 3, blank frames 2` (or `result: FAIL`). The check passes only when the picture size is right, the length matches the narration, there is audio, there are fewer than 2 camera jerks and no blank frames.

**Do:**
1. Check the pictures of that chapter: `novel sheet --chapter 21 --open`. A missing or damaged picture shows as a red box. Fix with `novel images --chapters 21` (or the NEEDS CHECK steps).
2. Render that chapter again: `novel build --chapters 21 --force`
3. Still failing? Try without the HD pictures: `novel build --chapters 21 --force --no-hd`
4. Look at just the start: `novel render --chapter 21 --end 30`

---

## Starting a new book

You need the book as an EPUB (or plain text files), your Flow project URL, and a Kaggle profile name.

**1. Make the book.**

```cmd
novel new
```

It asks: title, short folder name (letters, digits and `-`, e.g. `coi`), author, series label, chapters per episode, Google Flow project URL, Flow image model, narration voice, Kaggle profile and the disclaimer text. Press Enter to accept the answer in `[brackets]`. Ctrl+C stops with nothing created. At the end it shows a summary and asks to confirm. To answer on the command line: `novel new mybook --title "My Book" --chapters-per-episode 10 --yes`. It writes `book.yaml` and makes the book current.

**2. Put the EPUB in the book's `source` folder** (`D:\Novel Data\<book>\source\`).

**3. Extract the chapters.**

```cmd
novel extract
```

You see how many EPUBs and chapters were found, the first and last chapter, `gaps:`, `duplicates:` and the shortest chapters. Check first without writing: `novel extract --check`. If the book writes chapter titles in another way, add `book.chapter_regex` to `book.yaml` (a pattern with a group named `num` and, if you want, one named `title`). For a book split into volumes add `book.volumes`, a list of `[first chapter, last chapter, volume number]`.

**4. Make the first episode.**

```cmd
novel episode add
```

**5. Check the settings** the book needs and see where each value comes from:

```cmd
novel settings show --book mybook
```

At least set `images.flow_project`, `publish.title_template`, `publish.description_template` and `publish.research_seeds` in `book.yaml` (see `docs/settings.md`).

**6. Go through "One episode, start to finish" from step 3.** `novel write` translates the chapters that have no Hindi and locks them, then writes the bible and the pictures. (The by-hand "full" batch of `novel prompt` is **[FULL MODE: NOT BUILT]**.)

**7. Before the first `novel images`: the character pictures.** Once `novel write` has filled the bible, make and check the front views every picture uses as references: `novel ktrial up --chapter 1 --model qwen-image-turbo`, then `novel ktrial front --chapter 1` (see "Character pictures (front views)"). Do it again for characters that appear later (`--keys new_one,other`).

Migrating the old COI Audiobook project: `novel migrate coi --from "D:\COI Audiobook" --dry-run` shows the plan and writes nothing; without `--dry-run` it copies (it never changes the old folder); add `--with-media` to copy the finished videos too. Only the book `coi` can be migrated.

---

## Changing how the video looks

All of these are settings. Put them in:

| File | Applies to |
| --- | --- |
| `D:\Novel Data\<book>\book.yaml` | the whole book |
| `D:\Novel Data\<book>\episodes\E003\episode.yaml` | one episode |
| the `chapters:` block inside `episode.yaml` | one chapter (only the keys marked "chapter" below) |

Later layers win: chapter over episode over book over this PC over the built-in defaults. In YAML, indent with spaces (never tabs). To see what is in force, and where each value comes from:

```cmd
novel settings show --book coi --ep 3 --key video.logo
```

For a chapter: `novel settings show --book coi --ep 3 --ch 23 --key video.logo`. An invalid chapter key is refused with the list of allowed keys. Some changes (for example the logo) make the affected chapters render again on the next `novel build`.

### Disclaimer (the first card of every episode)

Keys: `video.disclaimer.enabled` (default true), `.seconds` (default 2.0), `.heading`, `.text` (empty = a standard text made from the book's title and author). Book or episode only; **not per chapter**.

Whole book, in `book.yaml`:

```yaml
video:
  disclaimer:
    seconds: 3.0
    heading: "COPYRIGHT & AI DISCLAIMER"
    text: "This is an unofficial, fan-made adaptation. Please support the official release."
```

One episode without the card, in `episode.yaml`:

```yaml
video:
  disclaimer:
    enabled: false
```

### Logo

Keys: `video.logo.enabled` (default false), `.file` (default `assets/logo.png`: `assets/...` is inside the repo, anything else is a full path), `.position` (`top-left`, `top-right`, `bottom-left`, `bottom-right`), `.margin` (pixels from the corner), `.opacity` (0 to 1), `.width` (pixels; empty = the picture's own size). The logo file must exist, or the build stops and tells you. **Chapter allowed.**

Whole book, in `book.yaml`:

```yaml
video:
  logo:
    enabled: true
    file: assets/logo.png
    position: top-right
    opacity: 0.8
    width: 160
```

One episode, in `episode.yaml` (a bigger logo in another corner just for this episode):

```yaml
video:
  logo:
    position: bottom-left
    width: 220
```

One chapter without the logo (chapter 23), inside `episode.yaml`:

```yaml
chapters:
  23:
    video:
      logo:
        enabled: false
```

### Subscribe cards

Keys:
- `video.subscribe.voiced.placement`: where the narrated card goes. `start-except-first-episode` (default: at the start of every episode but the first, which gets it at the end), `start`, `end` or `none`.
- `video.subscribe.silent.every_chapters` (default 5; 0 = none) and `.seconds` (default 5): the silent card after every N-th chapter (never after the last).
- `video.subscribe.silent_after`: **chapter only**: `true` forces a silent card after that chapter, `false` removes it.
- `video.subscribe.card_text`: the card's four lines (heading, button, reactions, tagline). `video.subscribe.spoken_lines`: the narrated message.

**Chapter allowed** for the `video.subscribe` block, but only `silent_after` does anything per chapter.

Whole book, in `book.yaml`:

```yaml
video:
  subscribe:
    voiced:
      placement: end
    silent:
      every_chapters: 4
      seconds: 4
    card_text: |-
      Enjoying the story?
      SUBSCRIBE
      Like  ·  Comment  ·  Share
      Help more fans find this channel
```

One episode (no silent cards at all), in `episode.yaml`:

```yaml
video:
  subscribe:
    silent:
      every_chapters: 0
```

One chapter (a silent card after chapter 23, none after chapter 25), inside `episode.yaml`:

```yaml
chapters:
  23:
    video:
      subscribe:
        silent_after: true
  25:
    video:
      subscribe:
        silent_after: false
```

After changing the card words, run `novel extras cards` again (and `novel extras voice` plus the narration commands if you changed `spoken_lines`).

### Captions (English subtitles)

Keys: `video.captions.enabled` (default true), `.size` (default 38), `.max_line` (characters per line, default 84), `.font_file`, `.font_name`. **Chapter allowed.** Captions are written by Gemini through your API key; with captions off you need no key.

Whole book, in `book.yaml`:

```yaml
video:
  captions:
    size: 42
    max_line: 70
```

One episode without captions, in `episode.yaml`:

```yaml
video:
  captions:
    enabled: false
```

One chapter (24) without captions, inside `episode.yaml`:

```yaml
chapters:
  24:
    video:
      captions:
        enabled: false
```

### Comic panels (a second look for the video)

The video can be drawn in two styles. `kenburns` (the default) shows one full-screen picture at a time. `panels` lays the chapter's pictures out as comic panels on pages: the camera rests on the panel being narrated, then slides to the next one. The pictures are the same ones; nothing is drawn again in Flow.

Try it on one chapter first. This never replaces the real chapter video, because the file gets the style in its name:

```cmd
novel render --chapter 21 --end 60 --style panels
```

You see `...\video\c0021-first60s-panels.mp4` and `result: PASS`.

To look at the layout of a whole chapter without rendering any video:

```cmd
novel render --chapter 21 --pages
```

It writes one picture per page into the episode's `video\c0021-pages` folder. Each panel shows its scene number (`s05` is scene 5).

To use the style for the whole book, add this to `book.yaml` (or to one episode's `episode.yaml`):

```yaml
video:
  style: panels
```

Then run `novel build`. Chapters that were drawn in the other style count as out of date and are rendered again.

A panel fills about 95% of the screen, and the next panel peeks in by only 1-2% at its edge. A page is one row of about four panels. There are two shapes: `wide` (the full 16:9 picture; three or more characters, establishing and wide shots) and `landscape` (4:3, one size below it; everything else). Two panels next to each other never have the same shape. The upright shapes (`portrait`, `tall`) and `square` are switched off in `novel/stages/episode_panels.py` (commented, not deleted); a scene's old `"panel"` key with one of those is drawn as `landscape`.

To fix one panel, edit that scene in the episode's `scenes\00021.json`, then render the chapter again:

- The panel shows the wrong part of the picture: change `"focus": [x, y]` to where the subject really is (0 to 1, measured from the top-left corner; `[0.7, 0.4]` is right of centre and a little above the middle).
- The shape does not suit the picture: add `"panel": "wide"` to the scene. The shapes are `wide`, `landscape`, `square`, `portrait` and `tall`.

Captions: in this style the English captions are lettered inside the panel being narrated, like the narration boxes of the Shorts reels (white boxes with a black border and shadow, capital letters, slightly tilted, popping in). A sentence is cut into short parts that fit the panel: upright panels get at most 4 words at a time, wide ones more. A caption sits at the top left of the panel, or at the bottom left when the top of the picture is much busier there (a face, a figure). There is no subtitle bar at the bottom in this style, so `video.captions.enabled: false` does not switch these off; `video.panels.captions: false` does, and so does `novel render --no-captions`. They need the English captions file, made the same way as for subtitles (`novel captions`).

Two more settings change the page: `video.panels.paper` (the page colour) and `video.panels.dim` (how much the other panels fade). They are described in `docs/settings.md`.

### Pictures with Qwen-Image on Kaggle (the default)

`novel images` draws every picture with **Qwen-Image-2.1-Turbo** (8 steps, 1376x768) in ComfyUI on Kaggle T4s: two notebooks, each its own server, fed from one shared queue on this PC. What makes the pictures consistent:

- **Picture lines** (`novel write`): with `images.engine: qwen` the IMAGES request carries the Qwen rules: a story someone tells is drawn as that story in its own places, not the room it is told in; every extra person gets a look of their own (age, build, hair, clothes); every expression names the mouth, the eyes and the brows, changes from picture to picture and is never only "calm" or "focused"; the action names the spot in the place and never names a bible person who is not in the cast. At most 2 named people per picture (`images.max_characters`).
- **References: the front views.** Each named person is sent with their approved front view, `<book>\bible\qwen\front\<key>_front.jpg` (one detailed full-body figure, 1088x1472; see "Character pictures" below). Someone without a front view falls back to their character sheet (`bible\qwen\<key>_sheet_seed202.jpg`), made first when missing (`novel ktrial charsheet --chapter N --missing`). A special outfit (a bible variant) uses the Flow portrait from `bible\flow\` instead.
- **The prompt** names each person once, in a block: `ONE named person, drawn once: X` or `TWO DIFFERENT people, each drawn once, clearly unlike each other: X is the ...; Y is the ...`, then one line per person (`[where: Name (reference) - look; pose; hands; wearing; face: mood]`); unnamed extras go "in the background, smaller and plainly different from them". It ends with: one single scene (no reference sheet, no multiple views), no text or bubbles, hands with five fingers, clean eyes with no white specks.
- **Picture QA**, only with `novel images --qa` (for unattended runs; normally a person looks at the pictures), right after each picture (OpenCode Go, `OPENCODE_API_KEY`): a panel (LongCat 2.5, free on Go, + Claude Haiku 5.5 on your Claude subscription, which reads the picture file in place; Qwen4 Max as spare) checks it; any fail goes to the referee (Claude Opus 5.5 on the subscription), who keeps it or lists every problem; the picture is redrawn once with those fixes (by whichever server is free) and the referee checks the redraw. The status list `qa.json` (next to the pictures, copied into the episode) says each picture's state, tries, server and findings. Without `--qa` there is no QA and no `qa.json`; `images.qa.*` in `docs/settings.md`. The models were chosen on 12 labelled chapter 1 pictures: Argon caught every duplicate person, Qwen4 Max 5 of 6, Haiku 4 of 6, LongCat 3 of 4, none of them failed a good picture.
- **Files**: `images\cNNNN\sNN-2k.jpg` in the episode, the prompt beside it as `sNN.prompt.txt`, the QA status list as `qa.json`. Each chapter works in `<book>\_work\ktrial\cNNNN\` (prompts, references, `server.json` / `server-2.json`).
- **HD**: `images.watermark: none` (Qwen pictures have no mark), so `novel hd` only upscales them to 1920x1080; nothing is cropped.

A picture that came out wrong (a face doubled, a wrong expression): `novel ktrial run --chapter 21 --only s12 --reroll` draws it again with the next seed in the work folder; then `novel images --chapters 21` copies it into the episode. Several at once without QA (free): `novel ktrial run --chapter 21 --only s12,s35,s38 --redo`.

**Two notebooks, one queue.** `novel ktrial up` (and `novel images` when no server answers) starts two notebooks, each on its own Kaggle account (`images.servers: 2`; Kaggle queues a second notebook on the same account until the first ends): `<book>-ktrial-comfy` and `<book>-ktrial-comfy-2`. Whichever server is free takes the next job (pictures, place plates, QA redraws, front views); a picture waits for its place plate; a server that stops answering hands its picture to the other, and the run never waits for a server that is still starting (it joins when it answers). One server alone still finishes the run.

**The addresses.** Notebook 1: the named Cloudflare tunnel `images.cf_hostname` (`coi-pipeline.prakashverma.com`), then the ngrok dev domain, then a Cloudflare quick tunnel. Notebook 2: the second named tunnel `images.cf_hostname_2` (`coi-pipeline-alt.prakashverma.com`), then a quick tunnel (no ngrok: its fixed domain serves one notebook). Each named tunnel's public hostname must point at `http://localhost:8189` in the Cloudflare dashboard. The tokens are private Kaggle datasets uploaded from `kaggle\CLOUDFLARE_TUNNEL_TOKEN` (`cloudflare-tunnel`) and `kaggle\CLOUDFLARE_TUNNEL_TOKEN_2` (`cloudflare-tunnel-2`), and the ngrok token from `kaggle\NGROK_AUTHTOKEN` (`ngrok-token`); a free account without them gets them uploaded. Never install the `cloudflared` service on this PC: the connector runs inside the notebook.

**Kaggle accounts.** The image servers and the narration (`kaggle\episode_tts.ipynb`, kernel `<user>/hindi-audiobook-daily`) are separate notebooks. With `kaggle.pick_free: true` each job takes the first account of `kaggle.profiles` (prakash, seema, shashi) that has no notebook running; `images.kaggle_profile` / `tts.kaggle_profile` is tried first, and `--account NAME` always wins. You see `Kaggle account seema: busy (seema12011/coi-ktrial-comfy)` and `Kaggle account shashi: free`. The second image notebook takes another free account (`images.kaggle_profile_2` first, never the first notebook's); with no free account left only one server starts. `novel ktrial down --chapter N` stops both at once (otherwise they stop after 20 idle minutes).

### Character pictures (front views)

The references above are made once per book, by hand, and checked by eye (no picture QA): when a new book is added, and when new characters appear.

```cmd
novel ktrial up --chapter 1 --model qwen-image-turbo
novel ktrial front --chapter 1
```

1. Characters without a `face` line first get one from Gemini, 8 per call so the faces differ: sex and age first (as the novel says), then face shape, cheekbones, nose, brows, eyes, lips, build, age marks and one telling feature; plus a resting expression. A bible look an image model would draw wrong is reworded, and the change is printed (`look reworded / was: ... / now: ...`); a call Gemini refuses is asked again one character at a time.
2. Each character is drawn at 1088x1472 with a seed of its own, as an ID photo: front view, head upright and level, eyes on the viewer, calm closed-mouth face (the resting expression is left out: smiles came out as a head tilt), arms at the sides, feet visible. A mounted rider is drawn alone on foot.
3. Look at `<book>\bible\qwen\front\`. Redraw the ones you do not like: `novel ktrial front --chapter 1 --keys martie,owl --redo` (the old picture goes to `front\old\`). A wrong person (wrong sex or age) is a bible problem: fix the character's `look` and `face` in `bible\characters.json` first.

(`--chapter` only names the folder whose server record is used. A command of its own for this is on the TODO list.)

### Picture size

Pictures are downloaded from Flow at 1K (1376x768) by default. The HD step then upscales each picture for the video after the Gemini mark is removed, so the video looks the same and the downloads are faster. To download at 2K (2752x1536) instead, set `images.quality: 2K` in `book.yaml`, or pass `--quality 2K` to `novel images` for one run.

### HD pictures

`novel build` makes the HD pictures by itself before it renders (when chaiNNer is installed). `novel render` does not: it uses HD pictures only if they already exist, and otherwise says `0 hd pictures, 40 cleaned downloads (no hd copy yet)`. To make them for one chapter, for example before `novel render`:

```cmd
novel hd --chapters 24
```

You see one line per group of pictures, such as `40 images 1376x768: cut 77 px bottom, 69 px each side -> 1238x691 -> 2x model -> 1920x1080`, and at the end `40 hd images in N s`. The pictures go to the episode's `images\c0024\hd` folder. Then `novel render --chapter 24` uses them.

Each picture gets the same treatment as in `novel build`: the Gemini mark is handled the way `images.watermark` says (`crop` cuts it off, `remove` runs `novel clean` first and uses its clean copies, `none` leaves it), a star painted into the picture gets a second trim, a 1K picture goes through chaiNNer's 2x model, and the result is exactly 1920x1080. Nothing is sent to an AI service; chaiNNer runs on this PC's graphics card.

Pictures that already have an up-to-date HD copy are skipped. To try it on a few first: `novel hd --chapters 24 --limit 5`. To redo them: `--force`.

### Pictures per chapter

Keys: `images.paced_seconds` (default 7.0) and `images.max_seconds` (default 7.5). Both may be set per chapter.

**Incomplete: these two settings do not change the number of pictures today.** They only change the wording of the Gemini prompt. The tool cuts each chapter with fixed numbers built into the code (a new picture about every 7 seconds of narration, never more than 7.5 s), and the renderer uses the same numbers. The examples below are valid YAML and are accepted, but do not rely on them yet.

Whole book, in `book.yaml`:

```yaml
images:
  paced_seconds: 6.0
  max_seconds: 6.5
```

One chapter (24), inside `episode.yaml`:

```yaml
chapters:
  24:
    images:
      max_seconds: 6.5
```

What does work, whole book or one episode (not per chapter): `images.max_characters` (default 4), the most named people allowed in one picture (used when importing and checking), and how much `novel images` does per run:

```yaml
images:
  max_characters: 3
  chapters_per_day: 2
  max_per_run: 120
```

### Removing the Gemini mark

Flow stamps a small star in the bottom-right corner of every download. The default, `images.watermark: crop`, cuts it off, which costs about a tenth of every picture. `images.watermark: remove` takes the star out with the npm package `@pilio/gemini-watermark-remover` instead (pinned in `flow/package.json`; `npm ci` in `flow` installs it, nothing else to set up; it works on your PC and sends nothing anywhere). Whole book, in `book.yaml`:

```yaml
images:
  watermark: remove
```

The honest picture, measured on 78 real pictures: the tool works on the 2752x1536 downloads in about three cases out of four (bright or textured backgrounds are clean; dark backgrounds can keep a faint dark star). On the 1376x768 downloads, which are most of E001 and E002, it almost never cleans the star: it misses the star's real position (it assumes a different one) or leaves a dark star-shaped ghost, and the tool has no option to tell it where the mark is. So every picture is checked after the tool ran, and a picture that still shows the mark, a ghost or a bright rim is **cropped instead**, exactly as `crop` does it. The copies in `images\cNNNN\clean\` therefore never show a mark, but they are a mix of full-frame and cropped pictures (both 16:9, so the video looks the same, only a bit more of the picture shows where removal worked). `clean.json` in that folder says which is which.

**Old pictures are never touched.** The default stays `crop`, so nothing changes for existing episodes; `remove` applies only to chapters cleaned from now on. `novel clean`, and the automatic step in `novel build` and `novel render`, refuse (and say why, and that the old pictures are left as they are) an episode marked `published: true`, anything under an `_archive` folder, and a chapter that already has a rendered chapter video or `clean\` / `hd\` pictures made the old crop way. Such a chapter keeps being built with `crop`. For an unpublished chapter only, `novel clean --chapters 21 --force` (with an explicit `--chapters`) lifts the last refusal. A published or archived episode is never cleaned.

To try it on a chapter and look at the result, with no video work:

```cmd
novel clean --ep 2 --chapters 11 --sheet --open
```

It prints, per chapter, `removed 41, cropped instead 20 (ghost 14, still marked 6), no mark 0` and opens `clean\sheet.html`: for every picture a before and after of the corner, enlarged, rejected pictures first. Pictures already cleaned are skipped (`--force` redoes them). After you set `images.watermark: remove`, `novel render` and `novel build` clean the chapters they render by themselves and use these copies; with `crop` they behave as before (the clean copies of `remove` are deleted when a `crop` render needs its own). Switching the setting makes the chapter videos render again; videos made before this setting existed count as `crop`. `none` leaves the mark in the video.

### Voice (narration)

Keys: `tts.voice` (default `Anirban`, a built-in voice of the narration model), `tts.kaggle_profile` (which Kaggle account narrates), `tts.poll_minutes` (how often the Kaggle run is checked, default 10). Book or episode; **not per chapter**. Chapters that already have audio keep their old voice until you narrate them again.

Whole book, in `book.yaml`:

```yaml
tts:
  voice: Anirban
  kaggle_profile: myprofile
```

One episode, in `episode.yaml`:

```yaml
tts:
  poll_minutes: 5
```

---

## Command reference

One entry per command. `[ ]` means optional. Every command that works on an episode takes `--book B` and `--ep N`; if you leave them out it uses the ones chosen with `novel use`. Chapter ranges look like `21-30` (or a single number).

### novel doctor
Checks this PC (Python, packages, Node, ffmpeg, Chrome, Kaggle, keys, fonts) and prints a FIX line for anything missing. Ends with `READY` or `NOT READY`.
- `--online` also tests Kaggle and the Gemini keys over the network
- `--fix-path` offers to add the repo's `bin` folder to your PATH
- `--json` print results as JSON

### novel settings
- `novel settings show [--book B] [--ep N] [--ch N] [--key PREFIX]`: every setting, its value and the layer it comes from. `--key` shows only keys that start with the prefix.
- `novel settings init --data-dir PATH [--chrome-exe P] [--chrome-profile P] [--cdp-url URL] [--chainner-exe P] [--chainner-model P] [--kaggle-account NAME --kaggle-username USER] [--force]`: writes this PC's settings file. Refuses to overwrite one unless `--force`.

### novel use
`novel use BOOK [--ep N]`: chooses the current book and episode.

### novel quota
`novel quota`: how many free-tier Gemini calls each key has made today and in the last minute (counted in `<novel home>/gemini_usage.json`, see `gemini.free_tier` in docs/settings.md). When every key has used its day, Gemini calls go to OpenRouter until midnight Pacific.

### novel new
`novel new [SLUG] [--slug S] [--title T] [--author A] [--series-label L] [--chapters-per-episode N] [--flow-project URL] [--flow-model M] [--voice V] [--kaggle-profile P] [--disclaimer-text T] [--yes]`: starts a new book. Every question has an option; `--yes` accepts the default for the rest.

### novel extract
`novel extract [--book B] [--source-dir DIR] [--out-dir DIR] [--check] [--force]`: turns the EPUBs in `source/` into chapter files. `--check` reports and writes nothing (exit 1 on gaps, duplicates or very short chapters). `--force` overwrites chapter files whose text differs.

### novel episode
- `novel episode add [--book B] [--chapters A-B] [--ep N]`: makes the next episode folder (or the given one) and makes it current.
- `novel episode list [--book B]`: one line per episode (Hindi, locked, audio, published).

### novel write
`novel write [--book B] [--ep N] [--chapters A-B] [--max-chapters N] [--model M] [--parallel N] [--dry-run] [--no-translate] [--redo N] [--fix N]`: the Gemini API writes the Hindi of chapters that have none (and locks it), the bible, and the picture lines, and files them with the importer. Without `--chapters` it takes the next `gemini.chapters_per_run` (5) chapters of the episode that need work; chapters that already have scenes are skipped and make no call. `--max-chapters` changes how many. `--model` overrides `gemini.api_model`. `--parallel` asks that many chapters at once. `--dry-run` shows the plan and request sizes and sends and writes nothing. `--no-translate` refuses chapters without Hindi. `--redo N` writes the scenes of chapter N again (old files move to `old\vN\`). `--fix N` sends the QA correction message of chapter N to the API and files the PATCH. `--translate N[,M...]` translates the listed chapters; a chapter that already has a translation keeps the old Hindi, audio, timing, captions, scenes, images and QA in `old\vK\` (nothing is deleted), and the new Hindi needs new narration (`novel tts push --chapters N`). `--restore N` puts back the latest set-aside version of chapter N (the current one is set aside first, so this can be undone) and makes no API call. Exit code 0 when every chapter of the run is complete.

### novel prompt
`novel prompt --chapters A-B [--book B] [--mode auto|full|scenes] [--copy] [--out DIR]`: writes the Gemini prompt and the files to upload. `auto` picks scenes when every chapter has locked Hindi and full when none has; a mix is refused. `--copy` puts the prompt on the clipboard. `--out` writes elsewhere and prints a warning (then `novel import` cannot find the batch; it is for looking at a prompt only).

### novel import
`novel import [--file F | --inbox] [--book B] [--batch ID] [--dry-run] [--replace] [--allow-revise]`: files a Gemini reply (clipboard by default) and prints what to say next. `--file` reads a saved reply, `--inbox` the files saved by hand in `inbox/`, `--batch B021-030` names the batch when it cannot be found, `--dry-run` checks and writes nothing, `--replace` replaces a chapter already imported, `--allow-revise` accepts BIBLE `revise` lines.

### novel qa
`novel qa [--book B] [--ep N] [--chapters A-B] [--reviewer claude|gemini|none] [--only-code] [--force-review] [--fix-prompt N [--with-warnings] [--copy]]`: checks Hindi and picture lists, asks an AI to review, writes `qa/report.md`. `--only-code` uses no AI. `--force-review` reviews again although nothing changed. `--fix-prompt N` prints the correction message for chapter N (`--with-warnings` adds warnings, `--copy` copies it).

### novel approve
`novel approve --chapters A-B [--book B] [--ep N] [--force] [--dry-run]`: locks the Hindi, writes the Flow prompt for each picture and the plan, and lists Flow Characters to create. `--force` approves although QA has not passed (recorded). `--dry-run` writes nothing.

### novel characters
- `novel characters list [--book B] [--json]`: the cast and which Flow Characters are made.
- `novel characters props [--book B]`: references that carry a held item.
- `novel characters sheet [--book B]`: writes `cast-sheet.md` in the book folder.
- `novel characters convert --from OLD.json [--places k1,k2] [--book B] [--force]`: converts an old character file to the current format.
- `novel characters make [--book B] [--ep N] [--keys k1,k2:variant,...] [--resend KEY] [--yes] [--i-checked-flow] [--dry-run]`: creates the Flow Characters in the visible Chrome. `--keys` limits which; `--resend KEY` makes one again after you looked in Flow; `--yes` skips the question; `--i-checked-flow` after a Flow stop; `--dry-run` lists only.

### novel chrome
`novel chrome [--dry-run] [--url URL] [--book B]`: starts the Chrome for Flow with your signed-in profile (never closes anything, never starts a second one on the same port). `--dry-run` shows the command; `--url` opens another page.

### novel run
`novel run [--book B] [--ep N] [--no-tts] [--no-captions] [--no-images] [--images-in-background] [--chapters A-B]`: starts narration and captions as background jobs, then makes pictures in this window. `--no-...` leaves a part out. `--images-in-background` runs pictures hidden too (a Flow stop then ends the run). `--chapters` is passed to the parts that accept it.

### novel jobs
- `novel jobs [--book B] [--ep N] [--all]`: the background jobs and their state (`--all`: every episode).
- `novel jobs --tail NAME [-n LINES] [--follow]`: the end of a job's log (`tts`, `captions` or `images`).
- `novel jobs --stop NAME`: stops a job (`images` stops cleanly after the current picture; the Kaggle run itself keeps going when you stop `tts`).

### novel tts
- `novel tts push [--book B] [--ep N] [--chapters A-B] [--account NAME] [--no-intro] [--dry-run]`: sends the locked Hindi of chapters without audio to Kaggle, and with them the episode's intro/recap (`extras\E9NNN`, from `novel intro init`) when it is locked and has no audio: one Kaggle run, one model load, and `tts pull` files the recap's audio in its own folder. `--chapters` or `--no-intro` leaves the intro out (then `novel tts push --ep 9NNN --chapters 1` still works on its own). With `kaggle.pick_free` it takes a free account (not the one running the image server). `--dry-run` stages and lists, calls nothing.
- `novel tts status [--book B] [--ep N]`: Kaggle's state of the run.
- `novel tts pull [--book B] [--ep N]`: brings finished audio and timing home.
- `novel tts watch [--book B] [--ep N] [--interval-seconds S]`: checks every few minutes and pulls when done.
- `novel tts reset [--book B] [--ep N] [--yes]`: gives up a run that ended in ERROR (or was cancelled) so `push` works again. Refused while the run is going or its output is waiting. Pulled audio is never deleted.

### novel captions
`novel captions [--book B] [--ep N] [--chapters A-B] [--force]`: writes the English subtitle text for locked chapters. `--force` redoes current ones. (A build makes missing captions itself when a Gemini key exists.)

### novel images
`novel images [--book B] [--ep N] [--chapters A-B] [--max N] [--qa] [--dry-run]` with `images.engine: qwen` (the default): draws every missing picture of the episode's chapters with Qwen-Image on Kaggle (starts two servers, each on its own free account, when none answers, feeds both from one queue, makes missing character sheets first, uses the front views as references), writes `images\cNNNN\sNN-2k.jpg` (each picture as soon as it is drawn); with `--qa` it also checks each picture with the picture QA as soon as it is drawn (a failed one is redrawn once) and writes the status list `qa.json`. `--max N`: at most the first N pictures of each chapter. `--qa`: the picture QA this run (off by default). `--dry-run`: lists what is missing, starts nothing. With `images.engine: flow`, `novel images [--book B] [--ep N] [--chapters A-B] [--max N] [--batch K] [--resend ID] [--upgrade] [--quality 1K|2K] [--i-checked-flow] [--dry-run]` makes pictures in Flow. Without `--chapters` it takes the next `images.chapters_per_day` chapters that have work. `--max` caps pictures this run. `--batch K` sends K prompts side by side. `--resend c0021-s05` sends one picture again on purpose. `--upgrade` upgrades 1K pictures to 2K. `--quality 2K` (or `1K`) sets the download size for this run only; the default is `images.quality`, which is 1K. `--i-checked-flow` after a Flow stop. `--dry-run` shows what it would do and the command, runs nothing.

### novel ktrial
The image server and the per-chapter work folder behind `novel images` (Qwen), also usable by hand for experiments (FLUX.3 klein, MiniMax-H3, Qwen). Everything is written to `<book>/_work/ktrial/cNNNN/`; only `charsheet` and `front` write to the bible (`bible\qwen\`, `bible\qwen\front\`, and the `face` lines in `bible\characters.json`).
- Picture QA: with `novel images --qa` (Qwen; off by default) every picture is checked through OpenCode Go (`OPENCODE_API_KEY` in secrets.env) as soon as it is drawn, while the next one is drawn: the panel (LongCat 2.5, free on Go, + Claude Haiku 5.5 on your Claude subscription; Qwen4 Max as spare) checks it; any fail goes to the referee (Claude Opus 5.5 on the subscription), who keeps it or lists every problem; the picture is then redrawn at once with those fixes and the referee checks the redraw. The status list is `qa.json` next to the pictures (copied into the episode with them), updated at every step: each picture's state (`queued`, `drawing`, `checking`, `redrawing`, `rechecking`, then `pass`, `kept`, `fixed`, `failed`, `unchecked` or `draw_failed`), its tries (1, or 2 after a redraw) and what QA found. Without `--qa` there is none; `images.qa.enabled: false` turns it off even with `--qa`.
- `novel ktrial qa --chapter N [--status]`: `--status` prints the status list (one line per picture: state, tries, what QA found). Without it: the picture QA by hand of the pictures that have no verdict yet (needs the server, for redraws).
- `novel ktrial qa-probe --chapter N [--picture FILE]`: sends one picture to every configured QA model and shows which answer.
- `novel ktrial front --chapter N [--keys a,b] [--redo]`: one detailed front view per character (default: everyone with a Qwen sheet) into `<book>\bible\qwen\front\<key>_front.jpg`, 1088x1472, each with its own seed, ID-photo head pose; these are the references of every picture. Characters without a `face` line first get one from Gemini (sex and age first, then face shape, nose, brows, build, age marks; plus a resting expression), 8 per call so the faces differ; a reworded bible look is printed. Drawn through the shared queue of both servers. No picture QA: character pictures are a manual step, checked by eye (see "Character pictures (front views)"). (`--chapter` only names the folder whose server is used.)
- `novel ktrial charsheet --chapter N [--keys a,b] [--missing]`: makes Qwen character sheets on the running Qwen server (`--keys`: these bible keys; `--missing`: every referenced person of chapter N without a sheet). Text only, from the bible look, seed 202, into `<book>\bible\qwen\<key>_sheet_seed202.jpg` (+ `manifest.json`).
- `novel ktrial scenes --chapter N [--book B] [--ep E] [--max-characters 2] [--from-production] [--force]`: asks Gemini for the chapter's picture lines with the close-framing rules. `--from-production` copies the existing scene file instead.
- `novel ktrial prompts --chapter N`: writes the prompts, the place-plate prompts, the character references and `plan.json`. No GPU.
- `novel ktrial up --chapter N [--account NAME] [--tunnel ngrok|cloudflared] [--domain HOST] [--idle-minutes 20] [--max-hours 6] [--gpu 1] [--comfy-arg=ARG] [--servers 1|2] [--dry-run] [--no-wait]`: starts the ComfyUI server on Kaggle. With your ngrok dev domain (`--domain`, remembered in `<kaggle home>/ngrok-domain`, or `NOVEL_NGROK_DOMAIN`) the address is fixed and `up` waits until the server answers; without one it prints how to find the address on the notebook page.
  - `up --model flux|minimax-h3|qwen-image-turbo`: which model the server loads (default FLUX.3 klein). `run` then writes to `<trial>\minimax-h3\` or `<trial>\qwen-image-turbo\`, so the FLUX pictures stay. Qwen-Image-2.1-Turbo (INT8, 17.3 GB of files, 8 steps) gets the place background as its first reference, because it edits `image_1`.
  - `up --ngrok-dataset SLUG`: takes the ngrok token from another private Kaggle dataset of the account (file `NGROK_AUTHTOKEN`; default `ngrok-token`), for a second ngrok account. Give that account's own `--domain`; it is not remembered and the stored one is left alone.
  - `up --servers 1|2` (default: `images.servers`, 2): with Qwen, `up` starts TWO notebooks, each its own server, on two different Kaggle accounts (the second: `images.kaggle_profile_2`, else the next free account; Kaggle queues a second notebook on the same account). The second one (`<book>-ktrial-comfy-2`) uses no ngrok: its address is the Cloudflare tunnel `images.cf_hostname_2` (the Alt tunnel, token `kaggle/CLOUDFLARE_TUNNEL_TOKEN_2`), then a quick tunnel. `up` waits for the first only; the second joins the shared queue when it answers (a one-line note says why only one starts when `images.cf_hostname_2` or the token file is missing). Records: `server.json` and `server-2.json`. The notebooks no longer start a second ComfyUI on the other T4 (it never fit in RAM).
- `novel ktrial url --chapter N ADDRESS`: stores the (first) server's address by hand when no fixed domain is set, then checks that it answers.
- `novel ktrial status --chapter N`: Kaggle's state, the speed probe and the address, for `server 1:` and `server 2:`.
- `novel ktrial down --chapter N`: stops every server.
- `novel ktrial run --chapter N [--only s05,g02] [--limit N] [--reroll] [--redo] [--plates-only] [--via kaggle|openrouter] [--resolution 1K|2K] [--budget D]`: makes the missing pictures (place plates first). `--reroll` tries the next seed, `--redo` the same seed again. With two servers `run`, `novel images` and `front` feed one shared queue: whichever server is free takes the next job, a picture waits for its place plate, a QA redraw jumps the queue, and a server that stops answering hands its picture to the other. A server that answers later joins by itself. With picture QA the status list (`qa.json`) records which server drew each picture (`server`).
- `novel ktrial report --chapter N`: `report.html`, each window's Flow picture beside the trial picture, with the panel cutouts.

### novel kllm
A coding LLM (Qwen3.6-35B-A3B, llama.cpp) on a Kaggle "GPU T4 x2" notebook, reached through a Cloudflare tunnel, for Pi or any OpenAI/Anthropic-style harness. It stops itself after 30 minutes without a request. State is global, in `<novel home>\kllm\server.json`.
- `novel kllm up [--account NAME] [--idle-minutes 30] [--max-hours 11.5] [--context 131072] [--thinking on|off] [--pad-kb N] [--smoke] [--dry-run] [--no-wait]`: pushes the notebook, waits for the server's address (it arrives through ntfy.sh), checks it answers and writes the address and key into Pi's `models.json` (provider `kaggle`). It tries the context 131072, then 65536, then 32768. `--smoke` is a CPU test run with a tiny model. Then: `pi --model kaggle/qwen-local`. Double-clicking `bin\llm-start.cmd` does the same.
- `novel kllm status`: Kaggle's state, the address, the context in use, uptime and minutes idle.
- `novel kllm url ADDRESS`: stores the address by hand if it did not arrive by itself (ntfy.sh down).
- `novel kllm down [--no-wait]`: stops the server now (so the idle wait does not use quota) and waits for the kernel to end. `bin\llm-stop.cmd` does the same.

### novel sheet
`novel sheet --chapter N [--book B] [--ep E] [--open]`: web page of one chapter's pictures with their text. `--open` opens it.

### novel clean
`novel clean [--book B] [--ep N] [--chapters A-B] [--force] [--sheet] [--open]`: removes the Gemini mark from the downloaded pictures of the chapters (default: every chapter of the episode that has pictures) into `images\cNNNN\clean\`, checks each result and crops the pictures where removal failed. Prints per chapter how many were removed, cropped instead (and why) or had no mark. `--force` redoes finished pictures. `--sheet` writes `clean\sheet.html`, a before/after of every picture's corner; `--open` opens it. Needs `npm ci` in `flow` once. Exit code 1 only if nothing could be done (no such chapter or episode, tool not installed).

### novel hd
`novel hd [--book B] [--ep N] [--chapters A-B] [--limit N] [--force] [--marks-only]`: makes the 1920x1080 copies of the pictures in `images\cNNNN\hd\` with chaiNNer (the same step `novel build` runs; default: every chapter of the episode that has pictures). The Gemini mark is handled as `images.watermark` says, 1K pictures are upscaled 2x. Up-to-date copies are skipped. `--limit N` does only the first N (a test). `--force` redoes them. `--marks-only` redoes only pictures that still show a painted star. Refuses an episode marked `published: true` or under `_archive`. Exit code 1 if nothing could be done (chaiNNer or its model missing, no pictures, a chapter not in the episode).

### novel status
`novel status [--book B] [--ep N] [--all] [--json] [--chapters A-B]`: the checklist. `--all` one line per episode of the book. `--json` machine-readable. `--chapters 21-25` limits the table and verdict. Exit code 0 only when ready to build, ready to publish or published.

### novel render
`novel render --chapter N [--book B] [--ep E] [--end SECONDS] [--style kenburns|panels] [--pages] [--no-hd] [--no-captions] [--out FILE]`: renders one chapter. `--end 30` renders only the first 30 seconds (a preview). `--style` draws it in that style whatever `video.style` says (the file is named `c0015-panels.mp4`, so it never replaces the real video). `--pages` writes the comic-panels layout as pictures (`video/c0015-pages/`) and renders no video. `--no-hd` uses the cleaned downloads. `--no-captions` leaves out subtitles. `--out` also copies the file there.

### novel build
`novel build [--book B] [--ep E] [--chapters A-B] [--force] [--when-ready] [--no-hd] [--per-hour N]`: renders what is not current and joins the episode. `--chapters` builds part of an episode (written as `E003-c21-25.mp4`). `--force` renders again. `--when-ready` waits for missing inputs, then renders at a steady pace (`--per-hour N`, default 2). `--no-hd` skips the HD step.

### novel intro
- `novel intro init --ep E --text-file F`: stores the Hindi hook (one paragraph per picture) in `extras\E9NNN`.
- `novel intro shots --ep E [--redo]`: Claude (the scene writer, then the usual fallbacks) writes `shots.json`: one shot per paragraph, up to `images.max_characters` named people from those with a front view, each with where, pose and face. A bad answer is asked again once with its problems listed; an existing `shots.json` is kept unless `--redo` (the old one is kept as `shots.before-<stamp>.json`).
- `novel intro images --ep E [--only s02] [--seed N] [--redo]`: Qwen pictures from `shots.json` on the running Kaggle ComfyUI.
- `novel intro render --ep E`: the clip, full screen with pan and zoom (Ken Burns), no title card; `novel build` puts it first.

### novel strip-heading
`novel strip-heading [--ep E] [--chapters A-B] [--dry-run]`: removes the spoken "चैप्टर ...: ..." heading from chapters
written before the change: the Hindi line, and when narrated the start of the mp3 (cut, no new Kaggle run), the timing,
captions and scene paragraph numbers. Originals go to `old\heading\`. New chapters are written without a heading
(the Gemini prompts no longer ask for one, and one that comes back is dropped). The title card shows only "Episode N"
on the first chapter.

### novel extras
- `novel extras voice [--book B]`: writes the subscribe message and prints the narration commands.
- `novel extras cards [--book B]`: builds the voiced and silent subscribe card videos.

### novel research
`novel research [--book B] [--refresh] [--seed TEXT] [--max-age-days N]`: collects YouTube search suggestions (needs `YOUTUBE_API_KEY`). `--seed` is repeatable; without it `publish.research_seeds` is used. `novel meta` runs this itself.

### novel meta
`novel meta [--book B] [--ep N] [--ai claude|gemini|none] [--refresh-research] [--teaser TEXT]`: title, description with timestamps, tags and thumbnail lines into `publish/`. `--ai none` needs `--teaser` and writes no thumbnail lines.

### novel thumb
`novel thumb [--book B] [--ep N] [--ch X-Y] [--hook "LINE|LINE"] [--image KEY_OR_PATH] [--focus x,y] [--zoom Z] [--face right|left] [--no-badge] [--no-tag] [--out DIR]`: thumbnails. `--hook` is repeatable (up to 3). `--image` is the picture: without it `thumbnail.art` is used, and without that the portrait of the character `novel meta` chose for the episode (`thumbnail_character` in `publish/publish.json`), if it exists. `--focus` defaults to `thumbnail.focus`, else `0.53,0.40`. `--ch` sets the chapter tag. `--no-badge` / `--no-tag` leave out the red EP badge / the yellow CH tag.

### novel published
`novel published [--book B] [--ep N] --url URL`: marks the episode as published and keeps the link. The link must look like a YouTube video link.

### novel migrate
`novel migrate BOOK --from OLD [--dry-run] [--with-media]`: copies the old COI Audiobook project into the new layout (only `coi`). Never changes the old folder.
