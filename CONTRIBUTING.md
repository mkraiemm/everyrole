# Contributing to EveryRole

Thanks for helping. Bug reports, flow-format ideas and pull requests are all welcome.

## Development setup

Python 3.12 or newer. Chrome and a model key are only needed to run flows for real, not for the test suite.

```bash
git clone https://github.com/mkraiemm/everyrole && cd everyrole
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]
```

The editable install puts both `everyrole/` and the vendored engine (`engine/jev_ultrafast`) on `sys.path`.

## Tests and lint

```bash
python -m pytest -q
python -m ruff check everyrole tests examples scripts
```

Both run in CI on Ubuntu and Windows with Python 3.12 and 3.13 (`.github/workflows/ci.yml`), along with a
build of the wheel that is installed in a clean virtualenv and smoke-tested.

The unit tests never call a model: the runner, access checker and suite take fakes for the agent, the browser,
the clock and the worker processes. Please keep it that way for new tests. A few tests start a real headless
Chrome (closing the tabs a killed worker left, Windows job objects holding a browser), so have Chrome or
Chromium installed; the Windows-only ones are skipped elsewhere.

New to the code? [AGENTS.md](AGENTS.md) has the layout, the architecture in a few lines and where to start for
common changes.

To try a change end to end, serve the demo app and run the quickstart (this calls the model provider, so it
uses a little of your credit):

```bash
python examples/quickstart/serve.py 8794
everyrole run examples/quickstart/flows --config examples/quickstart/everyrole.config.json \
  --base-url http://127.0.0.1:8794 --videos
```

## Code style

- `ruff` with a 120-character line limit (`pyproject.toml`); imports sorted.
- Small modules with one job each (`guard.py`, `writeguard.py`, `redact.py`, `verdict.py`...). A new check
  type is one class registered in `everyrole/assertions.py`; it never edits existing ones.
- Pass/fail must come from deterministic checks on the page, never from the model.
- A problem on the test machine must never be reported as an app failure: give every non-passing result a
  `cause` (see `everyrole/verdict.py`).
- No secret value may reach a model, a report or a log. Add a test when you touch anything on that path.

## Flow format changes

The schemas in `everyrole/schema/` are the source of truth. When you add or change a field:

1. update the schema (with a `description`, and a `default` where it has one);
2. document it in `docs/flow-spec.md` (`tests/test_docs.py` fails when a field or its default is missing);
3. add a line to `CHANGELOG.md` under "Unreleased".

## The vendored engine

`engine/jev_ultrafast` is a copy of [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT) at the
commit pinned in `engine/PATCHES.md`, with small patches. Rules:

- Do not reformat or refactor engine files; ruff skips `engine/` on purpose.
- Every change gets a row in `engine/PATCHES.md`: number, file, patch, and why. Keep patches small enough to
  offer upstream.
- To update the engine, copy the new upstream files, re-apply each patch from the table, update the pinned
  commit, and run the test suite plus a real quickstart run.

## Pull requests

- One topic per pull request, with tests.
- Describe the user-visible change and add it to `CHANGELOG.md`.
- Never commit `.env` files, reports (`everyrole-report/`), session files or real credentials. Use `example.com`
  addresses in tests and examples.

By contributing you agree that your contributions are licensed under the MIT License, and to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
