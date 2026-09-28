# EveryRole documentation

Read in this order the first time; every page also stands on its own.

| # | Page | What you learn |
|---|---|---|
| 1 | [Getting started](getting-started.md) | Install, check your machine, run the demo app's checks, then your first check on your own app |
| 2 | [Writing flows](writing-flows.md) | A tutorial: three flows, each harder than the last (public page, signed-in action, form without saving) |
| 3 | [Flow and config reference](flow-spec.md) | Every field of both file formats, checked against the JSON schemas by the test suite |
| 4 | [User types and access rules](user-types.md) | Signing in once per user type, sign-ins EveryRole cannot type, one flow for several user types, the no-AI access matrix |
| 5 | [Honest results](verdicts.md) | Failed vs. not checked vs. not run vs. passed on 2nd try; retries; exit codes |
| 6 | [Replays and reports](replays-and-reports.md) | The PR comment, `results.json`, JUnit, screenshots and replay videos |
| 7 | [GitHub Action](github-action.md) and [other CI](ci.md) | Running on every deploy: GitHub (Vercel previews), GitLab, any CI |
| 8 | [Security model](security.md) | What leaves your machine, the write-guard, forbid words, allowed hosts |
| 9 | [Bring your own AI key](ai-keys.md) | Jev through the Vercel AI Gateway or TypeSafe, the optional text helper, costs |
| 10 | [Troubleshooting](troubleshooting.md) | Chrome not found, timeouts, model provider errors, failing flows |
| 11 | [FAQ](faq.md) | Short answers to common questions |
| 12 | [Architecture](architecture.md) | How the engine, runner, suite, write-guard and verdicts fit together |
| 13 | [Contributing](../CONTRIBUTING.md) | Dev setup, tests, lint, engine patches |
| 14 | [AGENTS.md](../AGENTS.md) | The code base in one page, for AI coding agents and new contributors |
