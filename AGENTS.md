# AGENTS.md

Guidance for AI coding agents (and humans) working on this repository. Keep it accurate: when you change the
layout, a command or a convention below, update this file in the same pull request.

## What this project is

EveryRole is black-box end-to-end testing. A test ("flow") is a small JSON file with a goal in plain English
and deterministic checks. `everyrole run` opens the deployed app in a real, throwaway headless Chrome, signs in
once per kind of user ("user type", `personas` in the config), lets a small model (Jev) choose each click, and
judges pass/fail only from the checks on the loaded page. It also checks an access matrix (which user type may
open which page) without any AI, and reports the result as a pull-request comment, JUnit, JSON and replay
videos. It runs on the user's machine or CI with their own model key; there is no EveryRole server.

It is Python (3.12+), MIT licensed, with a vendored browser-driving engine in `engine/`.

## Layout

```
everyrole/            the package (CLI, suite, runner, checks, reports)
  schema/             JSON schemas for flow files and everyrole.config.json (source of truth for the formats)
  challenges.json     known security checks (CAPTCHAs), as data
engine/jev_ultrafast/ vendored, patched copy of jev-ultrafast (MIT); every change listed in engine/PATCHES.md
tests/                pytest suite
docs/                 user documentation (docs/README.md is the index); docs/media/ holds the logo, GIF, PNG
examples/             quickstart (demo app flows), a fictional SaaS config, a GitHub workflow
fixtures/             the demo app ("Acme Notes") served by examples/quickstart/serve.py
scripts/              render_pr_comment.py and make_demo_gif.py (the README GIF, from a real run)
action.yml            the GitHub Action
```

## Install, test, lint

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"      # the editable install puts everyrole/ and engine/ on sys.path
python -m pytest -q                    # the whole suite
python -m ruff check everyrole tests examples scripts
```

The tests never call a model. A few start a real headless Chrome (tab clean-up; Windows job objects), so have
Chrome or Chromium installed. Running flows for real (`everyrole run`) needs Chrome and `TYPESAFE_API_KEY`
(see `.env.example`); the demo app is served with `python examples/quickstart/serve.py 8794`.

## Architecture in a few lines

```
cli.py  ->  suite.py (SuiteRunner)  ->  one worker process per unit (worker.py: login | flow | access)
                                            |-> runner.py (FlowRunner) -> engine/jev_ultrafast (Agent + Browser) -> Chrome
                                            |-> access.py (AccessChecker, no AI)
                                            '-> signin.py (saved sign-ins, no AI)
        ->  verdict.py -> report.py (summary.md = PR comment, results.json, junit.xml) + video.py + usage.py
        ->  github.py (`everyrole comment`: one PR comment, updated in place)
ci.py (`everyrole ci`: an app's tests from EveryRole, logins from CI variables) -> `everyrole run` -> upload
```

- `cli.py`: commands `validate`, `run`, `signin`, `doctor`, `comment`, `ci`. `run` loads `.env` (`env.py`), the
  config and flows (`spec.py`, `site.py`), sets the run's memory/CPU caps (`limits.py`) and network pace
  (`pace.py`), starts Chrome (`chrome.py`), runs the suite, writes the report, returns the exit code
  (0 ok, 1 app failure, 2 nothing could be checked).
- `suite.py`: site-up pre-check (`reachability.py`), one sign-in per user type (sessions in a private temp
  folder, `session.py`), then flows and access batches in parallel, one retry per unit, live `progress.json`
  (`progress.py`). Every worker's browser daemon is registered and stopped (`daemons.py`, `processes.py`).
- `runner.py`: one flow. Hooks into the engine for the `forbid` guard and allowed hosts (`guard.py`), value
  names instead of secrets (`values.py`), redaction on the single outbound choke point (`redact.py`), the
  write-guard for form-only flows (`writeguard.py`), page settling (`pagewait.py`), checks (`assertions.py`),
  CAPTCHA detection (`challenge.py`), replay frames (`video.py`), and optional bug planting (`inject.py`).
- `verdict.py`: every non-passing result has a `cause` (`app`, `machine`, `setup`, `skipped`, `signin`) that
  decides failed / not checked / not run. Machine problems are never app failures.

Longer version: [docs/architecture.md](docs/architecture.md).

## Conventions

- **Tests are required.** Every behaviour change comes with a test; bug fixes with a test that failed before.
  Use fakes for the agent, browser, clock and worker processes (see existing tests); never call a model in a
  test. Do not weaken or delete a test to make a change pass.
- **Pass/fail comes only from deterministic checks** on the settled page, never from the model's "done".
- **No secrets in logs, reports or model requests.** A value from `{{secrets.NAME}}` must never reach a model,
  a report, a log line, a PR comment or an artifact. The model sees value *names*; `redact.py` scrubs everything
  that leaves. Add a test whenever you touch that path. Never commit `.env`, reports or session files.
- **Safety lives in code, not prompts**: `forbid` words, allowed hosts and the write-guard are enforced before
  every action. Form-only flows (`block_writes`) must never be able to save.
- **Black-box only.** EveryRole learns an app from its running pages, as a user would. Never add a feature that
  reads the tested app's source code, database or internal APIs.
- **Machine problems are not app failures.** Give every new failure path the right `cause`.
- **The schemas are the source of truth** for both file formats. Every schema field and default is documented
  in `docs/flow-spec.md`; `tests/test_docs.py` fails otherwise.
- **Data over hard-coded rules**: known security-check providers are data (`challenges.json`); settings and
  their defaults live in the schemas.
- **Engine changes are patches**: do not reformat or refactor `engine/` (ruff skips it); every change gets a row
  in `engine/PATCHES.md`.
- Style: `ruff`, 120 columns, sorted imports. Small modules with one job each. Use `example.com` addresses in
  tests and examples. Record user-visible changes in `CHANGELOG.md` under "Unreleased".

## Where to start for common changes

| Change | Start here | Also update |
|---|---|---|
| A new check kind (`expect`) | a class registered in `everyrole/assertions.py` | `flow.schema.json` (`$defs.check`), `docs/flow-spec.md`, `tests/test_assertions.py` |
| A new flow or config field | `everyrole/schema/*.schema.json`, then `everyrole/spec.py` | `docs/flow-spec.md` (enforced by `tests/test_docs.py`), `CHANGELOG.md` |
| A CLI option or command | `everyrole/cli.py` (and `worker.py` if workers need it) | `README.md` command table, docs, `action.yml` if the Action exposes it |
| How a unit is scheduled, retried or skipped | `everyrole/suite.py`, `everyrole/verdict.py` | `tests/test_verdict.py`, `tests/test_access_and_suite.py`, `docs/verdicts.md` |
| Sign-in behaviour | `everyrole/runner.py` (login flows), `everyrole/signin.py`, `everyrole/challenges.json` | `tests/test_signin.py`, `docs/user-types.md` |
| Access matrix | `everyrole/access.py` | `tests/test_access_and_suite.py`, `docs/user-types.md` |
| The PR comment, JUnit or results.json | `everyrole/report.py`, `everyrole/github.py` | `tests/test_report.py`, `tests/test_github.py`, `docs/replays-and-reports.md` |
| Memory/CPU caps or network pace | `everyrole/limits.py`, `everyrole/pace.py` | `tests/test_limits.py`, `tests/test_pace.py`, the `run` section of the config schema |
| Secrets and redaction | `everyrole/redact.py`, `everyrole/values.py` | `tests/test_safety.py`, `tests/test_values_and_engine.py`, `docs/security.md` |
| The GitHub Action | `action.yml` | `docs/github-action.md`, `examples/github/everyrole.yml` |
| The browser engine | `engine/jev_ultrafast/` | a row in `engine/PATCHES.md` |

Before you finish: run `python -m pytest -q` and `python -m ruff check everyrole tests examples scripts`, and
read your diff for secrets, local paths and private data.
