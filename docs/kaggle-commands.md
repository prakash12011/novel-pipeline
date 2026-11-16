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
