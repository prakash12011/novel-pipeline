# Novel Pipeline: context for Claude

A Python CLI (`novel`) that turns a web novel into narrated Hindi (Hinglish) YouTube episodes: Gemini writes the
Hindi and the picture lines, Qwen-Image on Kaggle draws the pictures, Maya narrates on Kaggle, ffmpeg builds the video.
The current book is COI (Circle of Inevitability); data lives in `D:\Novel Data\coi`, settings in
`%USERPROFILE%\.novel-pipeline\settings.yaml`, keys in `%USERPROFILE%\.novel-pipeline\secrets.env`.

## Read these first

| File | What it is for |
|---|---|
| `ISSUES.md` | **Every problem met so far, its cause and its fix**, and "Starting an episode": the order of work learned on episode 1. Read before changing prompts, picture rules, writers or Kaggle. Add each new kind of problem here. |
| `COMMANDS.md` | The run sheet: the daily routine, one episode start to finish, what each message means, the command reference. Keep it in step with every CLI change. |
| `docs/settings.md` | Every setting, its default and its scope (a test checks the table against `config/defaults.yaml`). |
| `TODO.md` | Planned work not started (character command, local QA on the 3060 Ti, curiosity hook). |
| `docs/kaggle-commands.md`, `docs/image-api-trial.md` | The Kaggle notebooks and the picture-model trials. |
| `README.md` | Install and overview. |

## How things run

- Run: `py -X utf8 -m novel <command>` with `PYTHONPATH` set to the repo (PowerShell: `$env:PYTHONPATH="d:/Novel Pipeline"`).
- Tests: `py -X utf8 -m pytest -q -p no:cacheprovider`. About 27 failures are old work in progress (headings, Phase titles,
  comic panels, kcomfy size steps, a render font, docs `strip-heading` and `video.panels.captions` scope): new work must not
  add to them. `tests/conftest.py` keeps tests away from real services (Gemini count, OpenCode, headless Claude).
- Who writes: Hindi and recap = free Gemini keys (`gemini-4-hydrogen`, 5/min and 20/day per key) -> Gemini 4 Argon on
  OpenCode Go -> OpenRouter. Scene lines = Claude Sonnet 5.5 through headless Claude Code (`claude -p`, the logged-in
  subscription) first, then the same chain (`novel/kclaude.py`, `novel/stages/episode_translate.py`).
- Pictures: `novel images` -> Qwen-Image-2.1-Turbo in ComfyUI on Kaggle notebooks (one per free account, each with its
  own named Cloudflare tunnel), one shared queue on the PC (`novel/stages/kpool.py`); prompts are built by code from the
  scene lines (`novel/kprompts.py`); references are the characters' front views (`bible/qwen/front/`). Picture QA is opt-in
  (`--qa`, `novel/kqa.py`): LongCat on OpenCode Go, Claude Haiku (panel) and Opus (referee) on the subscription
  (`claude:` models: headless Claude Code reads the picture file in place), Qwen4 Max as spare.

## Rules of the house

- Never print, log or commit a key or token; copy keys from the repo `.env` into `secrets.env` without echoing them.
  Token files in `kaggle/` are git-ignored (`NGROK_AUTHTOKEN*`, `CLOUDFLARE_TUNNEL_TOKEN*`).
- OpenCode Go's allowance is split per model and exhausting one model's share demotes the plan: say what a run will use
  before big loops on Go.
- Commit only when asked (branch `comic-panels`); never push unasked.
- A picture problem found by eye: find the cause in the prompt (`<chapter>/sNN.prompt.txt` next to the picture), fix the
  rule in code for every future picture, redraw only the bad picture, and note it in `ISSUES.md`.
- Hook and recap clips: full screen with pan and zoom (Ken Burns), never comic panels.
- YouTube text never mentions chapters: no "Chapter N", "Ch N-M" or chapter ranges in the title, description, tags, hashtags or thumbnails (thumbnails: `novel thumb --no-tag`). Timestamps are "Phase N". The templates in `book.yaml` (`publish.title_template`, `publish.description_template`) already leave them out; `novel/publish/meta.py` tells the AI the same.
- Windows: write scripts with the Write tool rather than shell heredocs (escaping breaks); PowerShell 5.1 has no `&&`.
