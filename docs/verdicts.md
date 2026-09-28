# Honest results

A red pull request should mean one thing: **your app is broken**. EveryRole keeps three situations apart
that most end-to-end tools lump together as "failed":

| Verdict | Meaning | Shown as | Turns the PR red? |
|---|---|---|---|
| **passed** | Every check held on the loaded page | ✅ passed | no |
| **passed on 2nd try** | The first try did not pass, the second did | ✅ passed on 2nd try, with a warning saying what went wrong the first time | no |
| **failed** | The app did not do what the flow expects | ❌ failed, with the failing check and the steps | yes (exit code 1) |
| **not checked** | EveryRole could not check: Chrome stopped answering, the model provider timed out, a worker hung; or a person must sign in (a CAPTCHA, a sign-in EveryRole cannot type, an expired saved sign-in) | ⚠️ not checked (machine problem, not your app), or "sign in yourself" | only if *nothing at all* could be checked (exit code 2) |
| **not run** | Skipped because something it depends on failed, e.g. its user type could not sign in | ⏭️ not run | no (the sign-in failure itself is reported) |

The logic lives in [`everyrole/verdict.py`](../everyrole/verdict.py). Every result that did not pass carries a
`cause`, set where the problem happened:

| `cause` | Verdict | Set when |
|---|---|---|
| `app` | failed | a check did not hold, the model gave up, a budget ran out, a `forbid` word or another host was hit, the site answered 5xx or not at all |
| `machine` | not checked | an exception inside the run (browser, model provider, worker crash or hang) |
| `setup` | failed | the flow or config file is invalid (caught earlier by `everyrole validate`) |
| `skipped` | not run | a dependency failed (sign-in) or the site was down |
| `signin` | not checked | a sign-in or test stopped on a security check (CAPTCHA), a sign-in never showed a password field, or a saved sign-in expired: a person must sign in ([user types](user-types.md#sign-ins-everyrole-cannot-type)) |

## Pass or fail comes from checks, never from the AI

The model only chooses the next click. When it says "done" or "I can't", EveryRole ignores the claim and reads
the page itself: the URL, the rendered text, the visible controls and (for form-only flows) the blocked save
attempts. A flow passes only when every `expect` check holds, after the page has stopped loading: text that
only exists on a spinner or skeleton screen ("Opening...") never passes a flow.

The reverse also holds: if the model errors out or gives up but the page already satisfies every check, the
flow passes (with a warning), because the app did its job.

## Retries

Every flow, sign-in and access page that does not pass is tried once more:

- a **machine problem** first heals the browser (EveryRole restarts its own Chrome if it stopped answering),
  then retries;
- an **app failure** is retried as is, so a one-off glitch does not turn the PR red;
- a **setup** error, a skipped result or a "sign in yourself" is not retried.

A result that passes on the second try is a pass, labelled "passed on 2nd try" with the first try's reason,
so flaky checks stay visible without blocking anyone.

## Before anything runs: is the site up?

`everyrole run` first requests `--base-url`. If the site does not answer (or answers 5xx) while the model
provider's address does answer, the site is down: that is reported as the single app failure and every
flow is "not run". If neither answers, the machine has no network: "not checked".

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Nothing broken found. Some checks may be "not checked" or "not run"; a warning says how many |
| `1` | At least one app failure |
| `2` | Nothing could be checked (machine problem), or EveryRole could not start: invalid flow file, Chrome not found, missing `TYPESAFE_API_KEY` |

The GitHub Action turns code 2 into a "could not check your app (not an app failure)" annotation; see
[github-action.md](github-action.md).

## Where results go

`everyrole run --output DIR` (default `everyrole-report/`) writes:

| File | Contents |
|---|---|
| `summary.md` | The pull-request comment: headline, one row per flow, steps of failing flows, warnings, access matrix |
| `results.json` | Everything: summary counts, each flow's status, cause, checks, steps, timing, tokens, warnings; each access row |
| `junit.xml` | App failures as `<failure>`, "not checked" and "not run" as `<skipped>`, for CI test views |
| `usage.json` | What the run used of the models, as the provider reported it: calls, tokens and cost per model |
| `progress.json` | The run's live state, rewritten as each unit starts and ends |
| `<flow>.jpg` | A screenshot of the page where a flow did not pass |
| `videos/<flow>.mp4` | With `--videos` and ffmpeg: a replay at real speed, ending on the page the checks were judged on |
| `videos/<flow>.jpg` | With `--videos`: that last page as a picture, for passing flows too |

All of it is scrubbed of secret values before it is written.
