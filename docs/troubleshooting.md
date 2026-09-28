# Troubleshooting

Start with `everyrole doctor`: it checks Python, Chrome, ffmpeg, your model keys and whether the model
provider answers, without calling any model. Every line that is not `ok` says how to fix it.

```
ok    Python: 3.12
ok    Chrome: /usr/bin/google-chrome
warn  ffmpeg (replay videos): not found: install ffmpeg to get replay videos
ok    Model keys for running tests: set
warn  Model keys for typing free text (optional): missing TEXT_MODEL_API_KEY, ... (put them in your .env file)
ok    Model provider reachable: https://ai-gateway.vercel.sh/typesafe/v1/systemone

Ready to test.
```

## "Chrome not found"

`error: Chrome not found. Install Chrome/Chromium or set CHROME_PATH.`

EveryRole looks for `CHROME_PATH`, then `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`,
`chrome` and `msedge` on `PATH`, then the standard install folders on Windows and macOS. Install Chrome or
Chromium, or point `CHROME_PATH` at the executable:

```bash
export CHROME_PATH=/usr/bin/chromium                       # Linux
export CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"   # macOS
set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe               # Windows
```

In Docker or other containers running as root, EveryRole adds `--no-sandbox` automatically.

To use a Chrome you started yourself (for example to watch a run with a visible window), start it with
`--remote-debugging-port=9222` and pass `--cdp-url http://127.0.0.1:9222` (or set `BU_CDP_URL`). EveryRole never
restarts a Chrome it did not launch.

## "Chrome DevTools did not answer"

EveryRole launched Chrome but could not talk to it within 20 seconds. Usually Chrome crashed on start: on
Ubuntu 24.04 the sandbox needs unprivileged user namespaces
(`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`); on a very small machine, it ran out of
memory (lower `--parallel`, or `run.limits.memory_percent`).

## Timeouts talking to the page ("timed out after 20s waiting for the daemon")

Each flow drives Chrome through a small helper process. Reading a very long page while several flows run in
parallel can take longer than the default 20 seconds. The result is reported as "not checked" (machine
problem) and retried once. If it keeps happening:

- lower `--parallel` (e.g. `--parallel 1`);
- raise the limit: `JEV_IPC_TIMEOUT_S=40`;
- give the machine more CPU (CI runners with 2 cores are slow with 3 parallel flows on heavy pages).

"worker hung (no answer after Ns)" means a flow went past its `timeout_s` plus a 30-second grace period; it is
also a machine problem and is retried.

## Model provider errors → "not checked"

`Model provider returned HTTP 401/403/404/429/5xx; no action executed.` or `Model connection failed` inside a
"⚠️ not checked" row means EveryRole could not ask the model, so it could not check your app. It never counts as
an app failure. Check:

| Symptom | Fix |
|---|---|
| HTTP 401 / 403 | Wrong key for the endpoint. A Vercel AI Gateway key needs `TYPESAFE_URL=https://ai-gateway.vercel.sh/typesafe/v1/systemone` and `TYPESAFE_MODEL=typesafe-ai/jev`; a TypeSafe key works with the defaults |
| HTTP 404 | `TYPESAFE_URL` or `TYPESAFE_MODEL` is misspelled |
| HTTP 429 | Rate limited. EveryRole already retries 429/503 twice with backoff; lower `--parallel` |
| Connection failed | No network, a proxy, or a firewall blocking the provider. `everyrole doctor` tells you if the address answers |
| `TYPE_TEXT needs TEXT_MODEL_API_KEY` | A flow had to type text that none of its `values` fits. Add a value for that field to the flow (preferred), or configure the text helper |

If every check is "not checked", `everyrole run` exits with code 2 ("EveryRole could not check anything"), and the
GitHub Action labels the job as a machine problem, not an app failure.

## "not run: sign-in as 'NAME' failed"

The user type could not sign in, so its flows and access checks were skipped. The sign-in itself appears as
`login@NAME` in the report with its failing check and a screenshot (and a video with `--videos`). Common
causes: wrong credentials in the environment, or `login.expect` that does not match what your app shows after
signing in. Try the sign-in alone with `everyrole signin --persona NAME`.

## "not run: 'NAME' needs you to sign in yourself"

The sign-in stopped on a security check (CAPTCHA), never showed a password field (Google/GitHub sign-in, an
emailed link or code), or its saved sign-in expired. EveryRole does not solve security checks and never counts
this as an app failure. Sign in by hand and give EveryRole the session:
[Sign-ins EveryRole cannot type](user-types.md#sign-ins-everyrole-cannot-type).

## "not run: the site did not answer"

`--base-url` did not respond (or answered 5xx) while the model provider did: your deploy is down. This is
reported as one app failure; nothing else runs.

## A flow fails but the app works

Open the replay video (`--videos`) or the screenshot next to `results.json`, and read the failing check in
the PR comment. Typical fixes:

- **The check is too strict**: `text_visible` compares text, ignoring case and whitespace; `element_visible`
  matches control labels. Use text that is really on the final page.
- **The page is slow**: raise `settle_s` so the checks are re-read while it finishes loading.
- **The model stopped early** ("agent blocked"): make the `goal` more concrete, e.g. name the button
  ("Open the Reports tab, then click Export CSV").
- **The button is below the fold**: EveryRole scrolls up to `scroll_retries` times when the model gives up.
- **Over budget**: raise `max_steps` or `timeout_s`.

## `error: ... needs env var NAME (from {{secrets.NAME}})`

A flow or user type uses `{{secrets.NAME}}` but the variable is missing or empty. Set it in the environment
or in `.env` (`--env-file` to use another file). In GitHub Actions, pass it with `env:` on the action step.

## The run is slower than expected, or says "paused"

A run holds itself to a budget (see [`run` in the reference](flow-spec.md)). Its first line says what it got,
e.g. `· limits: 8 GB memory and 50% CPU of 8 cores ...`, and its last one what it used, e.g. `limits: up to 3 at
once, ..., 2 held back until there was room`. Held-back work waited for memory or CPU; raise
`run.limits.memory_percent` / `cpu_percent` if the machine is dedicated to testing. Page loads are paced
(`run.network`): at most `pages_at_once` at a time, with gaps that follow how fast pages load. `· paused:
...; waiting for the network` means the app's address stopped resolving; nothing new starts until it does,
instead of every test failing.

## No replay videos

Videos need `--videos` and ffmpeg on `PATH` (or `FFMPEG_PATH`). Without ffmpeg the run still works; you get
screenshots only.

## Windows: garbled symbols in the terminal

The report uses emoji. If your console shows `?` or crashes with `UnicodeEncodeError`, set
`PYTHONUTF8=1` or use Windows Terminal.
