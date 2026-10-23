# Contributing to TickerLens

Contributions are welcome — from humans and from AI coding agents alike.
This project is deliberately structured so that an AI session can implement
a provider for *your* data source: **`CLAUDE.md` is the canonical contributor
manual** (Claude CLI users), with `GEMINI.md` as its mirror for Gemini users.
Read it first; it contains the capability contracts, the provider-addition
walkthrough, and the constitution.

## The non-negotiables (the "constitution")

1. **This app never predicts price direction and never computes buy/sell.**
   The one validated capability is the volatility band. Research stances come
   from the user's own dated research sessions — never from computed signals.
   PRs that add a computed recommendation will be declined regardless of
   quality.
2. **The API is read-only.** No order placement, ever.
3. **No secrets in files.** Credentials resolve via OS keyring →
   `~/.tickerlens/.env` (chmod 600) → env vars.
4. **User data is sacred.** `~/.tickerlens/` is never written by tests;
   anything touching a DB must set `TICKERLENS_DB_PATH` to a scratch path.
5. **Degrade, don't die.** Every provider raises `ProviderError`; every
   section renders ok/stale/unavailable. A dead API must never blank a page.

## Pull requests

- Backend: `cd backend && TICKERLENS_HOME=/tmp/tl TICKERLENS_DB_PATH=/tmp/t.db
  python3 -m pytest tests -q` — everything green, new behavior needs tests.
- Frontend: `cd frontend && npm run typecheck` — clean.
- Adding a data source? Follow the provider checklist in `CLAUDE.md`
  (provider file + `SUBSCRIPTIONS` entry + registry + TTL + docs row).
- Anything claiming statistical calibration must ship its validation
  evidence, like the existing `band_validation.json`.

## Your first PR walkthrough

1. Fork the repo and clone your fork locally.
2. Create a new branch for your changes (e.g. `git checkout -b fix/issue-number`).
3. Run backend tests and frontend typecheck to ensure everything is clean.
4. Open the pull request using the existing PR template.
5. Gotcha: GitHub only auto-closes issues when your PR description says `Fixes #3` with a space. `Fixes#3` without a space will not close the issue.
