# Vendored engine: jev-ultrafast

Upstream: https://github.com/browser-use/jev-ultrafast (MIT, see `LICENSE`)
Pinned commit: `1231850a0bf1a0c0341fe408ef1668dbbfdfac46`
Vendored files: `__init__.py`, `agent.py`, `browser.py`, `model.py`, `questions.py`, `snapshot.js`
(not vendored: `demo.py`, `static/`).

Keep patches small; each one should be offered upstream.

| # | File | Patch | Why |
|---|---|---|---|
| P1 | `model.py` | Jev endpoint from `TYPESAFE_URL` env (default stays `https://api.typesafe.ai/v1/systemone`) | Route through Vercel AI Gateway (gateway keys) or TypeSafe directly |
| P2 | `agent.py` | `Agent(..., max_steps, text_provider, before_execute)` | Per-flow step budget; secret-safe value typing; code-enforced `forbid` guard before any browser input |
| P3 | `model.py` | `OUTBOUND_FILTERS`: every request body passes through registered `str -> str` filters | Single choke point so secret literals (typed passwords echoed in history, page text) never reach any model |
| P4 | `snapshot.js` | Password inputs are offered as fill targets (not filtered, role = textbox); their value is reported as `[filled]`/`''`, fingerprints use length only | Upstream skips password fields entirely, so login forms were impossible; contents must still never leave the page |
| P6 | `browser.py`, `agent.py` | `Browser(url, isolated, prepare)`: optional own browser context (disposed on close) and a pre-navigation hook; `Agent(..., browser_options)` passes them | Parallel flows shared one cookie jar (a login leaked into every other flow); sessions must be restored before the first page load |
| P7 | `browser.py` | `Browser(..., settle)`: optional hook after the initial load, before the first observation | Apps that load data after `readyState=complete` (admin guard spinner) made the agent decide BLOCKED on an empty page |
| P8 | `agent.py` | `Agent(..., after_action)`: hook after each action, before the next observation | A slow navigation (6 s under load) was observed mid-flight; the agent saw the old page and gave up |
| P9 | `browser.py`, `agent.py` | Typing (`fill`) uses the same target-scoped freshness check as clicks | Pages with live content (ticker, rotating headline) changed the whole-page marker constantly: every typing decision was discarded until the model-call budget ran out |
| P10 | `agent.py` | `Agent(..., stop_needs_fresh_page)` (default True, upstream behaviour); everyrole passes False | DONE/BLOCKED make no input and everyrole judges outcomes itself; on pages with live content the stale check discarded every BLOCKED, burning the decision budget before scroll-recovery could run |
| P11 | `browser.py` | Page calls use `JEV_IPC_TIMEOUT_S` (default 20 s) instead of the harness's 5 s | Reading a 14,000 px page while three tests run in parallel timed out |
| P12 | `snapshot.js`, `browser.py` | Editable single-line inputs also offer `Press Enter in <label>` (kind `submit`): focus the input, press Enter | Apps that act on Enter (add a todo, search, chat) had no way to submit; the agent kept clicking the field |
| P13 | `browser.py` | On Windows, the harness's per-call local connections close with `SO_LINGER 0` (no TIME_WAIT) | One TCP connection per browser call, closed by the client, held a port for up to 4 minutes; page polling used up Windows' 16,384 outgoing ports and froze the whole machine's network |
