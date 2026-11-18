# TODO

- [ ] Curiosity hook: a short spoken teaser that opens an episode. Written by hand for episode 1 only (with chapter 1); not generated yet. Later: write it in the end-of-episode call (next to the recap), with its picture lines, and send it in the same `novel tts push` as the chapters and the recap.
- [x] Comic panels: at most 2 upright panels (portrait or tall) per page. Every single-character scene became an upright panel, so whole pages were rows of them. Fixed in `novel/stages/episode_panels.py` (`MAX_PORTRAIT_PER_PAGE`: extra upright panels become square, landscape or wide). Checked on COI chapter 24: every page now has 2 or fewer.
