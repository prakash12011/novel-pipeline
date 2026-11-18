# Settings reference

Every setting the tool reads from `config/defaults.yaml`: its default, what it does, and which layers may change it. To see what is in force right now and where each value comes from:

```cmd
novel settings show
novel settings show --book coi --ep 3 --key video.logo
novel settings show --book coi --ep 3 --ch 23 --key video.subscribe
```

## The layers

Lowest to highest. A higher layer wins.

1. **default**: `config/defaults.yaml` in the repo (do not edit; this page lists it).
2. **PC**: `~\.novel-pipeline\settings.yaml` (this PC only).
3. **book**: `D:\Novel Data\<book>\book.yaml`.
4. **episode**: `D:\Novel Data\<book>\episodes\E003\episode.yaml`.
5. **chapter**: the `chapters:` block inside `episode.yaml`, **only** for the keys marked chapter below.

Rules:

- Groups merge key by key: you write only the keys you change. **Lists and single values replace** the whole old value (a `tags:` list in `book.yaml` replaces the whole default list, it is not added to it).
- A chapter block may only contain the keys marked **chapter**. Anything else is refused with `cannot be overridden per chapter`.
- Missing files are fine; they simply add nothing.
- `null` means "not set". An empty list `[]` means "use the standard one" where the description says so.
- Paths starting with `assets/` are inside the repo; any other path is used as written.
- A chapter block looks like this (chapter number as the key):

```yaml
chapters:
  23:
    video:
      logo:
        enabled: false
```

Chapter scope is the whole `video.logo`, `video.subscribe` and `video.captions` blocks and `images.max_seconds`, `images.paced_seconds`. Be aware that the build only reads a per-chapter value of `video.logo`, `video.captions` and `video.subscribe.silent_after`; the other `video.subscribe` keys act on the whole episode.

Keys marked "Not read" or "Not used" are in the file for later; changing them does nothing today. The keys that nothing could honour yet (`images.min_seconds`, `images.max_ingredients`, `images.location_ingredients`) were taken out of the file; if an old `book.yaml` still has them, they are simply ignored.

## Keys in `config/defaults.yaml`

### Channel

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `channel.name` | `"Manga and Novel"` | The channel name. Written on the subscribe card and in the Gemini prompt. | default, PC, book, episode |

### Book

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `book.title` | `null` | The book's name, used in the Gemini prompt, the disclaimer, subscribe cards and YouTube text. Set by `novel new`. | default, PC, book, episode |
| `book.author` | `null` | The author's name, used in the standard disclaimer text. | default, PC, book, episode |
| `book.slug` | `null` | Name used to build Kaggle dataset and notebook names (`<slug>-e003-tts`). Empty = the book's folder name. | default, PC, book, episode |
| `book.series_label` | `null` | Series name on the chapter title cards and in the YouTube text. Empty = `book.title`. | default, PC, book, episode |
| `book.chapters_per_episode` | `10` | How many chapters `novel episode add` puts in one episode. | default, PC, book, episode |
| `book.volumes` | `[]` | List of `[first chapter, last chapter, volume number]`; labels chapters by volume (used in the YouTube text). Empty = one volume. | default, PC, book, episode |

### Narration (Kaggle)

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `tts.engine` | `maya2` | Narration engine. Only `maya2` works; anything else stops `novel tts push`. | default, PC, book, episode |
| `tts.model` | `maya-research/maya2-native` | Narration model name given to the Kaggle notebook. | default, PC, book, episode |
| `tts.voice` | `Anirban` | Narration voice (a built-in voice of the model). Chapters already narrated keep their old voice. | default, PC, book, episode |
| `tts.kaggle_profile` | `null` | Which Kaggle profile narrates. Empty = the PC's `kaggle.account`. `novel tts push --account` overrides it. | default, PC, book, episode |
| `tts.bitrate` | `128k` | Audio bitrate. Not used: the notebook always makes 128k, and `novel tts push` says so if you change it. | default, PC, book, episode |
| `tts.poll_minutes` | `10` | How often `novel tts watch` (and `novel run`) asks Kaggle whether the narration is finished. | default, PC, book, episode |

### Pictures (Flow)

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `images.engine` | `qwen` | What draws the pictures. `qwen`: Qwen-Image-2.1-Turbo in ComfyUI on a Kaggle GPU (`novel images` starts the server on a free account, writes `images/cNNNN/sNN-2k.jpg` and makes missing character sheets in `bible/qwen/`); `novel write` then adds the Qwen picture-line rules to its IMAGES request. `flow`: Google Flow in Chrome (the older way). | default, PC, book, episode |
| `images.kaggle_profile` | `null` | The Kaggle account the image server prefers. With `kaggle.pick_free` it is tried first and another free account is taken when it is busy; without it the narration's account (`tts.kaggle_profile`, then `kaggle.account`) is used. | default, PC, book, episode |
| `images.flow_project` | `null` | Address of the Google Flow project the pictures are made in. Required by `novel approve`; `novel chrome` opens it. | default, PC, book, episode |
| `images.flow_model` | `"Nano Banana Pro"` | The Flow image model; `novel chrome` reminds you to choose it in the prompt bar, and it is passed to the Flow scripts. | default, PC, book, episode |
| `images.allow_agent` | `false` | `true` lets the Flow scripts work with Flow's Agent mode on. Leave `false` (plain prompt bar). | default, PC, book, episode |
| `images.paced_seconds` | `7.0` | Aim: one new picture about every this many seconds of narration. **Only changes the wording of the Gemini prompt today**; the cutting code uses a fixed 7.0. | default, PC, book, episode, **chapter** |
| `images.max_seconds` | `7.5` | Longest a picture may stay on screen. **Only changes the wording of the Gemini prompt today**; the cutting code and the renderer use a fixed 7.5. | default, PC, book, episode, **chapter** |
| `images.chars_per_second` | `15.3` | Speaking speed (letters per second) used to estimate how long a text is spoken. **Only changes the wording of the Gemini prompt today**; the cutting code uses a fixed 15.3. | default, PC, book, episode |
| `images.max_characters` | `2` | Most named people (with a reference) allowed in one picture (Qwen keeps two faces apart reliably; Flow took 4). Used when importing and in QA. | default, PC, book, episode |
| `images.chapters_per_day` | `3` | How many chapters `novel images` takes per run when you do not give `--chapters`. | default, PC, book, episode |
| `images.max_per_run` | `200` | Most pictures one `novel images` run sends (`--max` overrides). | default, PC, book, episode |
| `images.batch` | `5` | Prompts sent to Flow side by side, then downloaded together (`--batch` overrides). 1 = one at a time. | default, PC, book, episode |
| `images.batch_pause` | `45` | Seconds to rest between batches when `images.batch` is more than 1. | default, PC, book, episode |
| `images.pause` | `3` | Seconds to wait between two pictures. | default, PC, book, episode |
| `images.chapter_pause` | `300` | Seconds to rest when the run moves on to the next chapter. | default, PC, book, episode |
| `images.rest_every` | `50` | After this many pictures the run rests for `images.rest` seconds. | default, PC, book, episode |
| `images.rest` | `120` | Length of that rest, in seconds (0 = never rest). | default, PC, book, episode |
| `images.wall_cooldown_hours` | `12` | After a Flow stop (limit, CAPTCHA, sign-in) `novel images` refuses to run for this many hours unless you add `--i-checked-flow`. | default, PC, book, episode |
| `images.quality` | `1K` | Size Flow downloads each picture in: `1K` (1376x768, the default) or `2K` (2752x1536). The HD step upscales a 1K picture after the Gemini mark is removed, so the video looks the same. `1K` pictures can be upgraded later with `novel images --upgrade`. `2K` can be forced for one run with `novel images --quality 2K`, or for a book or episode in its settings. | default, PC, book, episode |
| `images.alert_repeat_seconds` | `30` | When a run pauses for you (CAPTCHA, sign-in, limit) the tool beeps, then beeps again every this many seconds until you press a key. 0 = beep once. | default, PC, book, episode |
| `images.watermark` | `none` | Qwen pictures have no mark, so they are used as they are (the HD step only upscales). For Flow downloads: what to do about the Gemini star Flow stamps in the bottom-right corner of every download. `crop` cuts it off (about a tenth of the picture is lost; today's way). `remove` takes it out with the npm package in `flow/` (`novel clean`), checks the result and crops only the pictures where removal failed (mostly 1376x768 downloads: see "Removing the Gemini mark" in COMMANDS.md). `none` leaves the mark in. Changing it makes chapter videos out of date. | default, PC, book, episode |

### Video

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `video.fps` | `24` | Frames per second. **Not read: several parts of the renderer share a fixed 24, so changing this does nothing.** | default, PC, book, episode |
| `video.logo.enabled` | `false` | Show the logo on the video. The logo file must exist when this is `true`. | default, PC, book, episode, **chapter** |
| `video.logo.file` | `assets/logo.png` | The logo picture: `assets/...` is inside the repo, otherwise a full path. | default, PC, book, episode, **chapter** |
| `video.logo.position` | `top-left` | `top-left`, `top-right`, `bottom-left` or `bottom-right`. | default, PC, book, episode, **chapter** |
| `video.logo.margin` | `26` | Distance from the corner, in pixels. | default, PC, book, episode, **chapter** |
| `video.logo.opacity` | `1.0` | 0 (invisible) to 1 (solid). | default, PC, book, episode, **chapter** |
| `video.logo.width` | `null` | Logo width in pixels. Empty = the picture's own size. | default, PC, book, episode, **chapter** |
| `video.disclaimer.enabled` | `true` | Put the disclaimer card at the start of the episode. | default, PC, book, episode |
| `video.disclaimer.seconds` | `2.0` | How long the disclaimer card stays up. | default, PC, book, episode |
| `video.disclaimer.heading` | `"COPYRIGHT & AI DISCLAIMER"` | The card's heading. | default, PC, book, episode |
| `video.disclaimer.text` | `null` | The card's text; blank lines make paragraphs. Empty = a standard text made from the book title and author. | default, PC, book, episode |
| `video.subscribe.voiced.placement` | `start-except-first-episode` | Where the narrated subscribe card goes: `start-except-first-episode` (start of every episode but the first, whose card goes at the end), `start`, `end` or `none`. | default, PC, book, episode, **chapter** |
| `video.subscribe.silent.every_chapters` | `5` | A silent subscribe card after every this many chapters (not after the last). 0 = none. | default, PC, book, episode, **chapter** |
| `video.subscribe.silent.seconds` | `5` | How long a silent card stays up. | default, PC, book, episode, **chapter** |
| `video.subscribe.silent_after` | `null` | Chapter only: `true` forces a silent card after this chapter, `false` removes it, empty follows `every_chapters`. | default, PC, book, episode, **chapter** |
| `video.subscribe.card_text` | `null` | The card's four lines: heading, button, reactions, tagline. Empty = the standard card. | default, PC, book, episode, **chapter** |
| `video.subscribe.spoken_lines` | `[]` | The narrated subscribe message, one paragraph per item. Empty = the standard message. | default, PC, book, episode, **chapter** |
| `video.captions.enabled` | `true` | Burn English subtitles into the video (`kenburns` style; the `panels` style uses `video.panels.captions`). | default, PC, book, episode, **chapter** |
| `video.captions.font_file` | `assets/fonts/NotoSans-SemiBold.ttf` | Font file for subtitles (`assets/...` is inside the repo). | default, PC, book, episode, **chapter** |
| `video.captions.font_name` | `"Noto Sans SemiBold"` | The font's name as ffmpeg should call it. | default, PC, book, episode, **chapter** |
| `video.captions.size` | `38` | Subtitle font size. | default, PC, book, episode, **chapter** |
| `video.captions.max_line` | `84` | Most characters on one subtitle line. | default, PC, book, episode, **chapter** |
| `video.style` | `kenburns` | How chapter videos are drawn: `kenburns` shows one full-screen picture at a time with a slow camera move; `panels` lays the pictures out as comic panels and slides from one to the next. Changing it makes the chapter videos count as out of date, so `novel build` renders them again. | default, PC, book, episode |
| `video.panels.paper` | `"#f3f0e8"` | The page colour behind the panels (`panels` style only), as `"#rrggbb"`. | default, PC, book, episode |
| `video.panels.dim` | `0.45` | How far the panels that are not being narrated fade toward the page colour, 0 (not at all) to 1 (invisible) (`panels` style only). | default, PC, book, episode |
| `video.panels.captions` | `true` | English captions lettered inside the panel being narrated, in the Shorts reels' narration-box look (`panels` style only; there `video.captions.enabled` is not used). `false` = no captions. | default, PC, book, episode, **chapter** |
| `video.title_font` | `assets/fonts/PlayfairDisplay.ttf` | Font file for the chapter title cards (`assets/...` is inside the repo, otherwise a full path). Changing it makes the chapter videos count as out of date, so `novel build` renders them again. | default, PC, book, episode |
| `video.disclaimer_font` | `assets/fonts/NotoSans-Regular.ttf` | Regular font for the disclaimer and subscribe cards. | default, PC, book, episode |
| `video.disclaimer_font_bold` | `assets/fonts/NotoSans-SemiBold.ttf` | Bold font for the disclaimer and subscribe cards. | default, PC, book, episode |
| `video.hd.enabled` | `auto` | The HD picture step in `novel build`: `auto` (only if chaiNNer is installed), `true` (required), `false` (never). | default, PC, book, episode |

### Gemini API

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `gemini.api_model` | `gemini-3.5-flash-lite` | Gemini model used through your API keys: `novel write`, captions, QA fallback, `novel meta` fallback. | default, PC, book, episode |
| `gemini.chapters_per_run` | `5` | How many chapters one `novel write` run takes (the next chapters of the episode that need work). `--max-chapters` overrides it. | default, PC, book, episode |
| `gemini.parallel` | `1` | How many chapters `novel write` asks at the same time after the bible call. 1 is the safe choice; imports always happen one at a time. `--parallel` overrides it. | default, PC, book, episode |
| `gemini.max_attempts_per_block` | `3` | How many times `novel write` asks for one block (the Hindi of a chapter, the bible, the pictures of a chapter, a fix) before it leaves it as it is and goes on. | default, PC, book, episode |
| `gemini.fallback_models` | `[]` | Other Gemini models, tried in this order when every key is rate-limited for the main model (for example `[gemini-2.5-flash]`). The model that wrote each block is recorded in `progress.json` and in the scene file. | default, PC, book, episode |
| `gemini.min_seconds_between_calls` | `4` | Pause between two Gemini requests, so a run does not trip the per-minute limit. | default, PC, book, episode |
| `translate.target_words` | `800` | How many Hindi words one `novel write` narration should have. The rules ask for about this many, and the reply is checked against the band below. | default, PC, book, episode |
| `translate.words_tolerance` | `0.20` | How far from the target a narration may be: 0.20 with 800 words = 640 to 960 words. A reply outside the band is asked again with its word count; the last attempt is used only if it is within half to one and a half times the target. | default, PC, book, episode |
| `translate.max_attempts` | `4` | How many times `novel write` asks for the Hindi of one chapter before it gives up on it (the chapter then stays without Hindi). | default, PC, book, episode |

### Quality check

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `qa.reviewer` | `claude` | Who reviews chapters in `novel qa`: `claude`, `gemini` or `none`. The other one is the fallback (`qa.fallback`). | default, PC, book, episode |
| `qa.fallback` | `gemini` | The reviewer used when `qa.reviewer` fails. | default, PC, book, episode |
| `qa.parallel` | `3` | How many AI reviews run at the same time. | default, PC, book, episode |
| `qa.omission_ratio` | `0.45` | Warning when the Hindi is shorter than this share of the English text (letters without spaces): "possible omission". | default, PC, book, episode |

### YouTube text

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `publish.title_template` | `null` | YouTube title with `[N]`, `[X]`, `[Y]`, `[V]` (episode, first chapter, last chapter, volume). Required by `novel meta`. | default, PC, book, episode |
| `publish.description_template` | `null` | YouTube description template with the same placeholders. Required by `novel meta`. | default, PC, book, episode |
| `publish.tags` | `[]` | YouTube tags (a list; `[V]` is allowed). | default, PC, book, episode |
| `publish.hashtags` | `[]` | YouTube hashtags (a list, each starting with `#`). | default, PC, book, episode |
| `publish.playlist_url` | `null` | The series playlist link. Empty = `novel meta` leaves the playlist line out. | default, PC, book, episode |
| `publish.category` | `Entertainment` | YouTube category shown in the checklist. | default, PC, book, episode |
| `publish.research_seeds` | `[]` | Search phrases `novel research` and `novel meta` use to collect YouTube search suggestions (a list). | default, PC, book, episode |
| `publish.ai` | `claude` | Who writes the YouTube text in `novel meta`: `claude`, `gemini` or `none` (`none` needs `--teaser`). | default, PC, book, episode |

### Thumbnails

| Key | Default | What it does | Layers |
| --- | --- | --- | --- |
| `thumbnail.font` | `assets/fonts/Anton-Regular.ttf` | Font for the thumbnail lines. | default, PC, book, episode |
| `thumbnail.art` | `null` | Default picture for `novel thumb` (a path or a character key). `--image` overrides it. When both are empty, `novel thumb` uses the portrait of the `thumbnail_character` that `novel meta` chose, if that portrait exists. | default, PC, book, episode |
| `thumbnail.focus` | `null` | Face position `x,y` (two numbers from 0 to 1, for example `0.5,0.4`) that `novel thumb` uses when you do not give `--focus`. Empty = `0.53,0.40`. | default, PC, book, episode |
## Keys that are not in `config/defaults.yaml`

These are read by the tool but have no built-in default. Set them where shown.

- In the **PC** file (`~\.novel-pipeline\settings.yaml`, written by `novel settings init`):
  - `data_dir`: the folder that holds all books.
  - `chrome.exe`, `chrome.profile`, `chrome.cdp_url`: Chrome's program, the signed-in profile folder, and the DevTools address (normally `http://127.0.0.1:9222`).
  - `chainner.exe`, `chainner.model`: chaiNNer and its upscaling model (optional, for the HD step).
  - `kaggle.account` and `kaggle.profiles.<name>.username`: the default Kaggle profile and each profile's Kaggle user name.
  - `kaggle.pick_free` (PC settings): `true` makes the image server (`novel images`, `novel ktrial up`) and the narration (`novel tts push`) each take the first account in `kaggle.profiles` with no notebook running (the preferred one, `images.kaggle_profile` or `tts.kaggle_profile`, first), so two jobs never share an account's GPU. An account picked for the image server gets the ngrok token uploaded to its private `ngrok-token` dataset when it lacks it. `--account NAME` always wins.
- In **`book.yaml`**:
  - `book.chapter_regex`: how `novel extract` recognises a chapter heading, with a group named `num` and optionally `title`. Default: `Chapter 12`, `Chapter 12: Title`, `Chapter 12 - Title`.
