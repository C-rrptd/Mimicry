# Security Policy

## Supported versions

This is a small personal project with no formal release/version numbers —
security fixes only ever land on the `main` branch. There's no older
version being maintained in parallel, so "supported" effectively means
"whatever's currently on `main`."

## Reporting a vulnerability

**Please don't open a public issue for security vulnerabilities.** Since
this app runs locally and can be exposed to a network (see
`MIMICRY_ALLOW_LAN` in the README), a public report could point at a way to
compromise someone's running instance before a fix is out.

Instead:
- Use GitHub's **private vulnerability reporting** — go to the repo's
  **Security** tab → **Report a vulnerability**. This opens a private
  conversation only visible to the maintainer.
- If that's not available for some reason, reach out via the contact
  links in the README instead.

Please include:
- What the issue is and what it allows (e.g. "bypasses the app token
  check on X endpoint").
- Steps to reproduce.
- Your assessment of impact, if you have one — this is a single-user
  local tool, so the same bug can have very different real-world impact
  depending on whether `MIMICRY_ALLOW_LAN` is in play.

I'll do my best to respond promptly, but again — this is a personal
project maintained in spare time, not a funded security team, so please be
patient.

## Known security-relevant design points

Documented here so reports don't duplicate things that are already known,
intentional trade-offs (see the README's "Security" and "Known issues"
sections for more detail):

- **`settings.json` holds the app token in plaintext.** This is a known
  trade-off for a local, single-user tool — it's not designed to resist
  someone who already has file access to the machine it's running on.
- **`MIMICRY_ALLOW_LAN=1` sends the token over plain HTTP, no TLS.** Only
  intended for trusted home networks — this is documented, not something
  a report needs to point out again on its own.
- **The folder picker runs as a subprocess**, not in-process — this is a
  deliberate stability fix, not a sandboxing/security boundary.

If you find something that goes beyond these documented trade-offs —
particularly anything that lets someone bypass the app token entirely,
escape the download directory (e.g. via a crafted filename), or achieve
code execution — that's exactly what this policy is for. Please report it
privately rather than as a public issue.
