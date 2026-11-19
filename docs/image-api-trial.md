# Paid image APIs: cost trial (November 2026)

## Decision

- Date: 2026-11-16.
- Decision: stay with Google Flow for pictures. Planned pace: 3 chapters a day, 1 episode a week.
- The paid APIs are not used now. Revisit them when the channel is monetised. The trial code is kept in the repository so it can be run again.

## What was measured

- Sample: book `coi`, chapter 24 (episode 3). One chapter there is 44 pictures: 4 place pictures ("plates") plus 40 shots, with 84 reference images sent along with the shots (36 shots send 2 references, 4 shots send 3). Picture size 1376x768 (the 1K tier, 16:9). Prompts average about 1,400 characters.
- All prices are in US dollars, as read on 2026-11-16. They will change: re-check before spending.

## Cost of one chapter

| Model | Size / quality | Per picture | One chapter (44 pictures) | Basis |
|---|---|---|---|---|
| FLUX.3 klein via OpenRouter | 1K | $0.009 | $0.39 | actual run, 43 pictures |
| Nano Banana 2.1 | 1K | $0.0336 list, about $0.04 measured | about $1.80 | actual run of 13 pictures, extrapolated |
| Nano Banana 2.1 | 2K | $0.0504 | about $2.35 | list price, estimate |
| Nano Banana 2.1 | 4K | $0.113 | about $5.10 | list price, estimate |
| Nano Banana Pro | 1K or 2K | $0.134 | about $6.00 | list price, estimate |
| Nano Banana Pro | 4K | $0.24 | about $10.70 | list price, estimate |
| GPT Image 2.6 Flare | about 1K, low | $0.020 | $1.65 - $2.00 | third-party measurement, estimate |
| GPT Image 2.6 Flare | about 1K, medium | $0.034 | $2.30 - $2.60 | third-party measurement, estimate |
| GPT Image 2.6 Flare | about 1K, high | $0.041 | $2.60 - $2.90 | third-party measurement, estimate |
| GPT Image 2.6 Flare | xhigh | $0.074 | $4.00 - $4.35 | third-party measurement, estimate |
| GPT Image 2.6 Flare | max | $0.165 | $8.00 - $8.35 | third-party measurement, estimate |
| Google Flow | 1K / 2K | free with the Google account | $0 | what the pipeline uses today |

Notes on the table:

- GPT Image 2.6 Flare is billed by token (image output $30 per million tokens, image input $8 per million, text input $5 per million). OpenAI publishes no per-picture price. The Flare rows come from third-party measurements at 1536x1024, and no 16:9 figure was found. Reference images are expensive on Flare: about $0.009-$0.012 each, $0.70-$1.05 per chapter. That is the spread in the Flare rows.
- Nano Banana 2.1 is billed at $30 per million image output tokens and $1.50 per million input tokens. It also bills text and thinking output at $7.50 per million, which is why the measured price is above the list price. Reference images are cheap: about $0.001 each on Nano Banana Pro (Google's figure, 560 tokens).
- Nano Banana 2.1 has a batch price at half the standard price (about $0.80 per chapter at 1K). It is available only through Google's own API, and results are delivered later. Nano Banana Pro has no batch price. Neither model has a free API tier.
- OpenRouter adds no markup per request but charges a 5.5% fee when credits are bought.
- The list prices for Nano Banana were read from Google's pricing page partly through a summary. They match several independent write-ups. Re-check the page before committing money.

## The Nano Banana 2.1 run

- 13 pictures were made at 1K through OpenRouter: 10 shots plus the 3 place pictures those shots use as references. No failures.
- Total $0.53 ($0.3217 + $0.2057), about $0.04 per picture, about 19 seconds per picture. Every picture was 1376x768.
- Shots made, hardest first: s23, s10, s04, s37, s35, then s36, s38, s02, s20, s31. Place pictures: g01, g02, g03.
- What the pictures showed:
  - The two characters (Lumian and Aurore) looked the same across shots.
  - The two-person shots had exactly two people, with no duplicated person in the over-the-shoulder shots.
  - The mirrors in s23 and s35 reflected the right person.
- One mismatch: g02 and g03 are both named "Lumian's Room" but came out as different rooms (grey plaster and warm wood). So s23 and s31 do not match s35-s38.
- The pictures are in `<data_dir>\coi\_work\ktrial\c0024\nanobanana21\` (`images\` and `plates\`). The earlier FLUX.3 klein pictures stay in `...\c0024\openrouter\`.

## Speed

| Method | Per picture | Chapter 24 |
|---|---|---|
| Google Flow | about 67 s (range 42-103 s) | 90 minutes wall-clock for 40 pictures, about 43 minutes of actual work |
| Nano Banana 2.1 via API | about 19 s | about 14 minutes for 44 pictures, one at a time |

Notes:

- The Flow figures come from the save times of the chapter 24 downloads on 2026-11-15. They include typing the prompt, attaching the character, and generating and downloading through Chrome.
- The Flow run had one 48-minute gap near the end. Its cause is not recorded.
- Flow needs Chrome and the Google account open the whole time, and it can stop on a limit.
- API requests could run several at once. The current code sends one at a time.

## Watermark

- API pictures: Google's image guide says "All generated images include a SynthID watermark". This watermark is invisible. None of the 13 API pictures had a visible mark.
- Flow pictures: Flow's help page says a visible watermark is "applied automatically for users in India, South Korea, and Vietnam who are not on the Google One AI Ultra 20x Plan". Elsewhere it is a toggle under the profile picture.
- This is why the pipeline has the remove-or-crop step (`novel clean`), and why some Flow pictures lose about a tenth of the frame to the crop. With API pictures that step would not be needed.

## How to run the trial again

- The trial uses the existing `novel ktrial run --via openrouter` path. Two constants choose the model and the output folder:
  - `MODEL` in `novel/korouter.py`: now `google/gemini-nano-banana-2.1`, before `black-forest-labs/flux-3-image-klein`.
  - `OR_DIR` in `novel/stages/ktrial_run.py`: now `nanobanana21`, before `openrouter`.
- To try another model, change both, so the earlier pictures are not overwritten.
- It needs `OPENROUTER_API_KEY`, as an environment variable or a line in `secrets.env` in the novel home.
- Example command:

```
novel ktrial run --chapter 24 --via openrouter --resolution 1K --budget 0.60 --only s23,s10,s04,s37,s35
```

- `--budget` is the cap in dollars for one run. The default is 1.50, which is less than a full chapter costs on Nano Banana 2.1 (about $1.80). A full chapter therefore needs a higher value.
- A run makes the place pictures of the chosen shots first, so it makes a few more pictures than the shots named.

Model facts on OpenRouter:

| Model | OpenRouter id | Parameters |
|---|---|---|
| Nano Banana 2.1 | `google/gemini-nano-banana-2.1` | `resolution` 1K/2K/4K, `aspect_ratio` including 16:9, one picture per request, up to 14 reference images. `seed` is not listed. The run sent it and was not rejected, but whether it has any effect is unknown. |
| Nano Banana Pro | `google/gemini-3-pro-image` | `resolution` 1K/2K/4K, `aspect_ratio` including 16:9, one picture per request, up to 14 reference images. Not tried. |
| GPT Image 2.6 Flare | `openai/gpt-image-2.6-flare` | `quality` auto/low/medium/high/xhigh/max. Auto picked low in a third-party test, so send it explicitly. `aspect_ratio` including 16:9, up to 16 reference images. No `resolution` and no `seed` parameter, so the client would need small changes. Not tried. |

## Loose ends

- The price table in `novel/korouter.py` (`RESOLUTION_PRICE`, `COST_PER_IMAGE`) still holds the FLUX.3 klein prices. The real cost of each picture is read from the API's answer, so the totals are right. The budget guard's estimate for the next picture is too low, so it can overshoot by about one picture.
- `novel ktrial report` still labels the API column "FLUX.3 klein (OpenRouter)" whatever the model is. The report was not regenerated for the Nano Banana pictures.
- The tests in `tests/test_korouter.py` and `tests/test_ktrial_run.py` read the folder and the model from those two constants, so they pass with either model.

## Sources

- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API image generation guide](https://ai.google.dev/gemini-api/docs/image-generation)
- [Get started with Google Flow (watermark FAQ)](https://support.google.com/flow/answer/16353333)
- [OpenRouter pricing](https://openrouter.ai/pricing)
- [OpenRouter, Nano Banana 2.1](https://openrouter.ai/google/gemini-nano-banana-2.1)
- [OpenRouter, Gemini 3 Pro Image](https://openrouter.ai/google/gemini-3-pro-image)
- [OpenRouter, GPT Image 2.6 Flare](https://openrouter.ai/openai/gpt-image-2.6-flare)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
- [mxchat, measured GPT Image 2.6 token counts and costs](https://mxchat.ai/gpt-image-2-pricing-2-6-test/)
- [dev.to, measured cost per edit on fal](https://dev.to/lygerone/gpt-image-26-pricing-what-one-edit-really-costs-on-fal-measured-5aan)
