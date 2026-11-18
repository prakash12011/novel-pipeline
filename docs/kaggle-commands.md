# Kaggle picture trial: commands

Pictures for one chapter from the open model FLUX.3 klein, run in ComfyUI on a Kaggle T4.
The Flow character portraits are the references; FLUX draws the scenes and the backgrounds.

Everything is written to `<book>/_work/ktrial/cNNNN/` (for COI chapter 24:
`D:\Novel Data\coi\_work\ktrial\c0024\`). The episode and the bible are never changed.

Run the commands from `D:\Novel Pipeline`. If `novel` is not on your PATH, type `py -X utf8 -m novel` instead.
Replace `24` with the chapter you want.

## Once, before the first run

| What | Where |
|---|---|
| Accept the FLUX.3 klein license | `https://huggingface.co/black-forest-labs/flux-3-image-klein-comfy`, with the account behind the `hf-token` Kaggle dataset |
| ngrok token | private Kaggle dataset `ngrok-token`, file `NGROK_AUTHTOKEN` (already created) |
| Your fixed ngrok address | `https://dashboard.ngrok.com/domains` shows one name like `something.ngrok-free.dev`. Give it once with `--domain` (step 2); it is remembered. |

## 1. Scene lines and prompts (no GPU)

```powershell
novel ktrial scenes --chapter 24
novel ktrial prompts --chapter 24
```

- `scenes` asks Gemini for the picture lines with the close-framing rules (at most 2 people). Add `--force` to write
  them again. `--from-production` copies the episode's existing scene file instead of asking Gemini.
- `prompts` writes the prompts, the place-plate prompts, the character references and `plan.json`. Read a few
  `images\sNN.prompt.txt` files before spending GPU time.

## 2. Start the server on Kaggle

```powershell
novel ktrial up --chapter 24 --domain something.ngrok-free.dev    # first time
novel ktrial up --chapter 24                                      # afterwards
novel ktrial status --chapter 24
```

`up` waits until the server answers, about 7 minutes. `status` should end with "the server answers".

Without a fixed address, `up` cannot learn the address by itself (Kaggle shows a notebook's log only after it ends).
Then open the notebook page it prints, wait for the line `KTRIAL URL https://...` and store it:

```powershell
novel ktrial url --chapter 24 https://PASTE-THE-ADDRESS
```

## 3. Generate

```powershell
novel ktrial run --chapter 24 --plates-only            # the backgrounds first: check plates\gNN.jpg
novel ktrial run --chapter 24                          # every picture that is missing
```

Fixing pictures:

```powershell
novel ktrial run --chapter 24 --only s05,s07 --reroll  # a new seed for rejected pictures
novel ktrial run --chapter 24 --only g02 --reroll      # a new background and all pictures of that group
novel ktrial run --chapter 24 --only s05 --redo        # the same seed again (after a prompt or reference change)
novel ktrial run --chapter 24 --limit 6                # only the first six
```

A run can be stopped with Ctrl+C and started again; finished pictures are kept. Replaced pictures go to `images\old\`.

## 4. Check, then stop

```powershell
novel ktrial report --chapter 24
novel ktrial down --chapter 24
```

- `report` writes `report.html` in the trial folder: each window's Flow picture beside the trial picture, with the
  panel cutouts. Run it again after more pictures are made.
- `down` stops the server. It also stops by itself after 20 idle minutes or 6 hours. GPU quota is used the whole
  time it is up.

## Other models: `up --model`

```powershell
novel ktrial up --chapter 24 --model qwen-image-turbo --account prakash --ngrok-dataset ngrok-token-2 --domain SECOND.ngrok-free.dev
novel ktrial run --chapter 24 --plates-only      # then without --plates-only
```

- `qwen-image-turbo`: Qwen-Image-2.1-Turbo (7B, 8 steps, INT8; 17.3 GB from `Comfy-Org/Qwen-Image-2.1`, not gated, Qwen
  Research License). Pictures go to `<trial>\qwen-image-turbo\` (plates, images, log, with the prompt as sent), so the
  FLUX pictures stay. Qwen edits its first reference, so the place background is sent first and the prompt is
  renumbered (`<image1>` is the place). `QW_REF_RESOLUTION` in `novel/kcomfy.py` sets the size the references are
  resized to (768 is a guess; tune it from the first run's seconds). The notebook tries one card with no arguments,
  then `--fp32-unet`, until a probe picture is not blank.
- `minimax-h3`: MiniMax-H3-Image (GGUF) on both cards.
- `--ngrok-dataset SLUG`: the private dataset of the account that holds the ngrok token (file `NGROK_AUTHTOKEN`), for a
  second ngrok account (one free ngrok account allows one tunnel at a time). Give that account's `--domain`; it is not
  remembered. Without one the address is not fixed and you copy it from the notebook page (`novel ktrial url`).
- Only one server is recorded per chapter (`server.json`): `up` refuses while the recorded one is running, so run
  `novel ktrial down` for the FLUX server first.

## Where things are (chapter 24)

| What | Path under `D:\Novel Data\coi\_work\ktrial\c0024\` |
|---|---|
| Pictures | `images\sNN.jpg` (prompt in `sNN.prompt.txt`, seed and seconds in `sNN.meta.json`) |
| Backgrounds | `plates\gNN.jpg` |
| Character references sent to the model | `refs\` |
| Comparison page | `report.html` |
| Server address and state | `server.json` |

## Measured on a Kaggle T4 (chapter 24)

| Step | Time |
|---|---|
| Server start to ready | about 7 minutes |
| One background | about 24 s |
| One picture with a character and a background reference | about 36 s |

## Known limits

- Kaggle's rules on tunnels were not confirmed.
- ngrok's free plan allows 1 GB out and 20,000 requests a month.
- A background decides the place for its whole group: check the plates before making the pictures.
- A character's `look` text in the bible is sent together with the portrait. Where the two disagree, FLUX follows the text.

# Coding LLM on Kaggle: commands

A free fallback co-pilot for simple pipeline tasks: Qwen3.6-35B-A3B (Q4_K_M) served by llama.cpp on a Kaggle
"GPU T4 x2" notebook, reached through a Cloudflare quick tunnel. It is started on demand and stops itself.

```powershell
novel kllm up            # or double-click bin\llm-start.cmd
pi --model kaggle/qwen-local
novel kllm status
novel kllm down          # or bin\llm-stop.cmd: do this when you are done
```

- `up` makes a fresh API key, uploads a small private dataset (`kllm-session`: the key, the limits, `novel/kllm.py`),
  pushes the kernel `kllm-server` (it also attaches your `hf-token` dataset) and waits for the address. The notebook
  installs llama.cpp (pinned build), downloads the model from Hugging Face every start (22 GB, a few minutes), tries
  context 131072, then 65536, then 32768, opens the tunnel and posts only the address to ntfy.sh under a random topic.
- Then `up` writes the address and key into `~\.pi\agent\models.json` as provider `kaggle`, model `qwen-local`, and
  leaves your other providers alone. The key lives only in that file, in the private session dataset and in
  `<novel home>\kllm\server.json`.
- The server ends by itself after 30 minutes without an inference request (`--idle-minutes`), on `down`, after
  11.5 hours (`--max-hours`) or if llama-server dies. Status pings do not keep it alive. Each session can use up to
  30 idle minutes of GPU quota unless you run `down`.
- If the address does not arrive (ntfy.sh down), `up` says so and prints the notebook page; copy the line
  `KLLM URL https://...` from its log and run `novel kllm url ADDRESS`.
- `--thinking on` lets the model reason before answering (slower). `--pad-kb` sets the streaming padding below.
- `--smoke` runs the same notebook on a CPU kernel with a tiny model (no GPU quota) to test the plumbing.

## Streaming through the tunnel

Measured on a CPU smoke run through a real Cloudflare quick tunnel: a streamed reply (400 events, 99 KB) arrived live,
first bytes after 0.7 s and the last after 17.7 s, with no padding. Padding of 65 KB or 160 KB made no difference to when
the text arrived, so it is off by default. `--pad-kb N` turns it on: the proxy then sends that many bytes of ignorable
comment lines first and a comment line every 20 s while llama-server is silent. That would only matter for a prompt
read that stays silent for about 120 s (a quick tunnel answers HTTP 524 after that); at the measured 1,300 tokens a
second this needs a prompt of about 150,000 tokens, more than the largest context.

## Known limits

- Kaggle's rules on tunnels were not confirmed.
- ngrok's free plan allows 1 GB out and 20,000 requests a month.
- A background decides the place for its whole group: check the plates before making the pictures.
- A character's `look` text in the bible is sent together with the portrait. Where the two disagree, FLUX follows the text.

# Coding LLM on Kaggle: commands

A free fallback co-pilot for simple pipeline tasks: Qwen3.6-35B-A3B (Q4_K_M) served by llama.cpp on a Kaggle
"GPU T4 x2" notebook, reached through a Cloudflare quick tunnel. It is started on demand and stops itself.

```powershell
novel kllm up            # or double-click bin\llm-start.cmd
pi --model kaggle/qwen-local
novel kllm status
novel kllm down          # or bin\llm-stop.cmd: do this when you are done
```

- `up` makes a fresh API key, uploads a small private dataset (`kllm-session`: the key, the limits, `novel/kllm.py`),
  pushes the kernel `kllm-server` (it also attaches your `hf-token` dataset) and waits for the address. The notebook
  installs llama.cpp (pinned build), downloads the model from Hugging Face every start (22 GB, a few minutes), tries
  context 131072, then 65536, then 32768, opens the tunnel and posts only the address to ntfy.sh under a random topic.
- Then `up` writes the address and key into `~\.pi\agent\models.json` as provider `kaggle`, model `qwen-local`, and
  leaves your other providers alone. The key lives only in that file, in the private session dataset and in
  `<novel home>\kllm\server.json`.
- The server ends by itself after 30 minutes without an inference request (`--idle-minutes`), on `down`, after
  11.5 hours (`--max-hours`) or if llama-server dies. Status pings do not keep it alive. Each session can use up to
  30 idle minutes of GPU quota unless you run `down`.
- If the address does not arrive (ntfy.sh down), `up` says so and prints the notebook page; copy the line
  `KLLM URL https://...` from its log and run `novel kllm url ADDRESS`.
- `--thinking on` lets the model reason before answering (slower). `--pad-kb` sets the streaming padding below.
- `--smoke` runs the same notebook on a CPU kernel with a tiny model (no GPU quota) to test the plumbing.

## Streaming through the tunnel

A Cloudflare quick tunnel holds a streamed reply back until about 140 KB has passed or the reply ends, and cuts a
silent reply after about 120 s. The proxy can send that much ignorable padding (SSE comment lines) at the start of a
streamed reply and a comment line every 20 s while llama-server is silent. The default is set by a measurement:
see "Measured" below.

## Known limits

- Start-up is about 4 to 6 minutes plus Kaggle's queue (once 25 minutes).
- Running this server and the ComfyUI picture kernel at the same time is untested.
- The model is small for agent work: let it run read-only commands first.
