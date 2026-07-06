# Contributing to Mimicry

Thanks for considering contributing! This is a small personal project (see
the "Vibe-coded" note in the README), so the process here is intentionally
lightweight.

## Before you start

For anything beyond a small fix — a new feature, a significant refactor, a
new supported site — **open an issue first** to discuss it before writing
code. That avoids spending time on a PR that might not fit the project's
direction, or that duplicates something already planned.

For small, obvious fixes (a typo, a clear bug with an obvious one-line fix,
a broken link in the README), feel free to just open a PR directly.

## Reporting bugs

Open an issue and include:
- What you did, what you expected, and what actually happened.
- Your OS and Python version.
- The relevant error message or traceback, if any (check the terminal
  running `app.py` — most errors surface there, not just in the browser).
- Whether it's reproducible, and with what (a specific playlist/link, if
  you're comfortable sharing it — no need to if it's private).

Check the README's "Known issues" section first — your bug might already be
a documented limitation rather than something new.

**Security issues** (anything related to the app token, auth, or exposing
something you shouldn't) — see `SECURITY.md` instead of a public issue.

## Suggesting features

Open an issue describing the use case, not just the feature — "I want X
because Y" is more useful than "add X," since there might be a simpler way
to solve Y that doesn't require building X.

## Pull requests

- Keep PRs focused — one fix or feature per PR is much easier to review
  than a bundle of unrelated changes.
- Test your change actually works before submitting (there's no automated
  test suite yet, so manual verification is what we've got for now).
- Match the existing code style — this project favors explanatory comments
  for non-obvious decisions (see the existing code for examples) over
  terse, uncommented logic.
- Update the README if your change affects setup, features, or known
  limitations.
- Describe what you changed and why in the PR description — "fixes #12" is
  good, but a sentence on the actual approach helps review go faster.

## Adding support for a new site

Most of the heavy lifting (actually resolving and downloading media) is
already handled by `yt-dlp` — if `yt-dlp` supports a site, this app likely
already can download from it via a pasted link, just under a generic "web"
badge. Adding a proper badge/name for a new site is usually just a couple
of entries in `EXTRACTOR_TO_SITE` and `classify_site()` in `app.py` — no
new download logic needed. If a site genuinely doesn't work even though
`yt-dlp` supports it, that's more likely a bug worth its own issue.

## Development setup

Same as the README's Setup section — there's no separate dev-only setup.
