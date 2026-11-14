# Novel Pipeline

`novel` is a command-line tool that turns a web novel into long Hindi/Hinglish "novel explained" episodes for the YouTube channel "Manga and Novel".

- You run it yourself, one command at a time, from a terminal. `novel status` always tells you what is missing and which command fixes it.
- Gemini (in the browser, by hand) writes the pictures' descriptions; Google Flow draws the pictures in your own signed-in Chrome; Kaggle narrates; ffmpeg renders.
- Book data (chapters, pictures, audio, videos) lives in a separate data folder, not in this repo.
- Per-PC settings and keys live in `~\.novel-pipeline\`.
- The day-to-day instructions are in [COMMANDS.md](COMMANDS.md). Every setting is listed in [docs/settings.md](docs/settings.md).

## Set up on a new PC

Do the steps in order. After each one, the text under "You should see" tells you it worked. Commands are for Windows (PowerShell or Windows Terminal).

### 1. Install the programs

Install these first (normal installers; tick "add to PATH" where asked):

- Python 3.11 or newer (3.14 is what this PC uses). Check: `py --version`
- Node.js 18 or newer. Check: `node --version`
- Git.
- Google Chrome (the normal Windows install).
- ffmpeg, the **full build** (it must include "libass"): for example the "full" build from gyan.dev, or `winget install Gyan.FFmpeg`. It must be on PATH. Check: `ffmpeg -version`
- Optional: Claude Code (`claude` on PATH). Without it, `novel qa` and `novel meta` use Gemini instead.
- Optional: chaiNNer, for the HD picture step. Without it the HD step is skipped.

### 2. Get the code

```cmd
git clone <the address of your private repo> "D:\Novel Pipeline"
```

You should see a new folder `D:\Novel Pipeline` with `bin`, `novel`, `flow`, `kaggle` and `config` inside.

(If the code is not on GitHub yet, copy the whole folder from the old PC instead; skip `node_modules` and `__pycache__`, they are rebuilt.)

### 3. Install the Python packages

```cmd
py -m pip install -r "D:\Novel Pipeline\requirements.txt"
```

You should see `Successfully installed ...` (or "already satisfied" for each package).

### 4. Install the picture helper (Node)

```cmd
cd /d "D:\Novel Pipeline\flow"
npm ci
```

You should see `added N packages`. This installs `playwright-core`, which lets the tool drive your Chrome.

### 5. Tell the tool about this PC

Create an empty Chrome profile folder for the tool, for example `D:\Novel Pipeline Chrome`, then run (use your own paths; the Kaggle names come from step 7):

```cmd
"D:\Novel Pipeline\bin\novel.cmd" settings init --data-dir "D:\Novel Data" --chrome-exe "C:\Program Files\Google\Chrome\Application\chrome.exe" --chrome-profile "D:\Novel Pipeline Chrome" --kaggle-account <profile-name> --kaggle-username <kaggle-username>
```

You should see `wrote C:\Users\<you>\.novel-pipeline\settings.yaml`. That file holds the data folder, Chrome's program and profile folder, the DevTools address (`http://127.0.0.1:9222`) and the Kaggle names. It is for this PC only; do not copy it to another PC. To change something later, run the same command again with `--force`. Add `--chainner-exe "<path>"` if you installed chaiNNer.

To type just `novel` from any folder:

```cmd
"D:\Novel Pipeline\bin\novel.cmd" doctor --fix-path
```

It offers to add `D:\Novel Pipeline\bin` to your PATH. Say yes, then open a new terminal.

### 6. Keys: `secrets.env`

Copy `secrets.env.example` from the repo to `%USERPROFILE%\.novel-pipeline\secrets.env`, and fill in the values. The file has these names (never put the values anywhere else, never commit this file):

- `GEMINI_API_KEY` (needed: English captions, and the fallback AI)
- `GEMINI_API_KEY2`, `GEMINI_API_KEY3` (optional extra keys; the tool tries the others when one is rate-limited)
- `YOUTUBE_API_KEY` (optional: search suggestions for `novel research`)

### 7. Kaggle (narration)

1. Make or reuse a Kaggle account. In Kaggle: **Settings, API**, create an access token.
2. Save it as the file `%USERPROFILE%\.kaggle\access_token-<profile-name>` (the profile name you gave in step 5).
3. In Kaggle, make a **private dataset** named `hf-token` that holds your Hugging Face token. The narration notebook reads it from there; it never lives on this PC.

### 8. Chrome and Flow

```cmd
novel chrome
```

You should see `Chrome answers on port 9222 (...)`. In the Chrome window that opened: sign in to Google and Flow, open the book's Flow project, and in the prompt bar choose one 16:9 image with the model Nano Banana Pro. This is your own profile, shown on screen; the tool never uses a hidden browser and never closes it. Close any other Chrome that uses the same profile folder first (two Chromes cannot share one profile).

The Flow "Characters" (saved references) belong to your Google account and Flow project, so they are already there on a new PC.

### 9. Run the check until it says READY

```cmd
novel doctor
```

Each line starts with `[R]` (required) or `[o]` (optional) and shows `ok`, `MISSING`, `warn` or `skipped`. A `FIX:` line under a line tells you what to do. Fix, run it again, repeat until the last line says `READY: ...`. Warnings (for example `youtube api key`) do not stop you. To also test Kaggle and the Gemini keys over the network:

```cmd
novel doctor --online
```

### 10. Pick the book and look at it

```cmd
novel use coi --ep 3
novel status
```

(If the book data is not on this PC yet, do the next section first.)

## Run

Call the tool with `"D:\Novel Pipeline\bin\novel.cmd" <command>`. After `novel doctor --fix-path` and a new terminal, you can type `novel <command>` from any folder.

### One episode in short

1. Pick the book and episode:

   ```cmd
   novel use coi --ep 3
   ```

2. See what is missing:

   ```cmd
   novel status
   ```

3. Make the Gemini prompt (it is copied to the clipboard):

   ```cmd
   novel prompt --chapters 21-30 --copy
   ```

4. File Gemini's reply (repeat for each chapter):

   ```cmd
   novel import
   ```

5. Check the work:

   ```cmd
   novel qa --chapters 21-30
   ```

6. Approve the chapters:

   ```cmd
   novel approve --chapters 21-30
   ```

7. Open Chrome for Flow:

   ```cmd
   novel chrome
   ```

8. Start narration, captions and pictures:

   ```cmd
   novel run
   ```

9. Make more pictures on later days:

   ```cmd
   novel images
   ```

10. Build the video:

    ```cmd
    novel build
    ```

11. Write the title, description and tags:

    ```cmd
    novel meta
    ```

12. Make the thumbnails:

    ```cmd
    novel thumb --image lumian_lee
    ```

### Every day

Run these three commands, in this order:

```cmd
novel status
novel images
novel status
```

The full step-by-step run sheet is in COMMANDS.md.

## Moving book data between PCs

**One PC works on a book at a time.** Two PCs working on the same episode at once will overwrite each other's files and will both talk to Flow and Kaggle. Finish (or stop) on one PC before you start on the other.

What to copy: the whole book folder, for example `D:\Novel Data\coi\` (everything inside: `source`, `chapters`, `bible`, `gemini`, `episodes`, `extras`, `research`, `inbox`, `book.yaml`). Use an external drive or a synced folder; check that the copy is complete before you delete anything on the old PC.

What **not** to copy: `~\.novel-pipeline\` (it holds this PC's paths, `state.json` and your keys), `~\.kaggle\` (tokens: save a token on each PC), and the Chrome profile folder (sign in again).

After copying:
1. Put the folder under the `data_dir` of the new PC (or change it: `novel settings init --data-dir "<path>" --force` plus your other options).
2. Run `novel use coi --ep 3`, then `novel status`. It checks the files and says what is missing.
3. The picture plan of an episode (`flow\plan.json`) written by the current `novel approve` stores paths relative to the episode folder, so it works at any folder path: nothing to do. A plan written by an older version stores full file paths; if the path is different on the new PC, run `novel approve --chapters 21-30` once for each approved chapter range of each unfinished episode: it rewrites the whole plan with relative paths and keeps the pictures you already have.
4. Background jobs and the Chrome lock are per PC; nothing to move.

## Where things live

| What | Where | Notes |
| --- | --- | --- |
| The tool (this repo) | `D:\Novel Pipeline\` | `bin\novel.cmd` is the command; `novel\` is the code; `flow\` the Chrome/Flow scripts; `kaggle\` the narration notebook; `config\defaults.yaml` the built-in settings; `assets\fonts\` the fonts |
| Book data | `D:\Novel Data\<book>\` (path set by `data_dir`) | `book.yaml`, `source\` (EPUB), `chapters\`, `bible\`, `gemini\<batch>\` (prompt, files to upload, raw replies), `inbox\`, `research\`, `extras\`, `episodes\E003\` |
| One episode | `D:\Novel Data\<book>\episodes\E003\` | `episode.yaml` (settings for the episode), `hindi\`, `scenes\`, `images\`, `audio\`, `timing\`, `captions\`, `video\` (renders and the joined `E003.mp4`), `publish\` (title text and thumbnails), `qa\`, `flow\` (plan, journal, status) |
| This PC's settings | `%USERPROFILE%\.novel-pipeline\settings.yaml` | data folder, Chrome, Kaggle names |
| This PC's keys | `%USERPROFILE%\.novel-pipeline\secrets.env` | key names only in this repo; see `secrets.env.example` |
| Current book and episode | `%USERPROFILE%\.novel-pipeline\state.json` | what `novel use` writes |
| Kaggle tokens | `%USERPROFILE%\.kaggle\access_token-<profile-name>` | one per profile |
| Chrome profile | the folder you gave as `--chrome-profile` | signed in to Flow |

Set the environment variable `NOVEL_HOME` to use another folder than `~\.novel-pipeline` (useful for tests).

## Running the tests

```cmd
cd /d "D:\Novel Pipeline"
py -m pytest -q
```

`tests\test_docs.py` checks that every command and option written in `COMMANDS.md` and `README.md` really exists, and that `docs\settings.md` and `config\defaults.yaml` list the same keys.
