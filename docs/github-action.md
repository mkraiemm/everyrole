# GitHub Action

The action runs your flows against a deployed URL, posts one pull-request comment (updated in place on every
run), writes the job summary, and uploads the report with a replay video of every flow.

## On every Vercel preview deploy

Vercel (like Netlify, Render and Railway) reports each deployment to GitHub. A successful one fires the
`deployment_status` event with the deployment's URL. Copy
[`examples/github/everyrole.yml`](../examples/github/everyrole.yml) to `.github/workflows/everyrole.yml`:

```yaml
name: everyrole
on:
  deployment_status:

permissions:
  contents: read
  pull-requests: write
  issues: write
  deployments: read

jobs:
  flows:
    if: github.event.deployment_status.state == 'success' && github.event.deployment_status.environment_url != ''
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.deployment.sha }}
      - uses: mkraiemm/everyrole@v0
        with:
          base-url: ${{ github.event.deployment_status.environment_url }}
          environment: ${{ startsWith(github.event.deployment.environment, 'Production') && 'production' || 'preview' }}
        env:
          TYPESAFE_API_KEY: ${{ secrets.TYPESAFE_API_KEY }}
          TYPESAFE_URL: ${{ vars.TYPESAFE_URL }}
          TYPESAFE_MODEL: ${{ vars.TYPESAFE_MODEL }}
          QA_EMAIL: ${{ secrets.QA_EMAIL }}
          QA_PASSWORD: ${{ secrets.QA_PASSWORD }}
```

The comment is attached to the pull request whose head commit was deployed (looked up from the commit).

Preview deployments behind Vercel Authentication (Deployment Protection) show a Vercel sign-in page to
EveryRole's browser, and every check fails. EveryRole cannot send Vercel's automation-bypass header yet: turn
Deployment Protection off for preview deployments, or point `base-url` at an unprotected staging URL.

## On any other trigger

```yaml
on:
  pull_request:
jobs:
  flows:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: mkraiemm/everyrole@v0
        with:
          base-url: https://staging.example.com
        env:
          TYPESAFE_API_KEY: ${{ secrets.TYPESAFE_API_KEY }}
```

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `base-url` | *(empty)* | The deployed URL to test; `{{BASE_URL}}` in flows. Required without `app` |
| `flows` | `flows` | Flow files or directories, space-separated, relative to the repository root |
| `config` | *(empty)* | Path to `everyrole.config.json`. Empty = `./everyrole.config.json` if it exists |
| `environment` | `preview` | Only flows whose `run_on` includes this run (flows without `run_on` always run) |
| `parallel` | `3` | Flows run at the same time |
| `videos` | `true` | Record a replay video of every flow (ffmpeg is installed on Linux runners when missing) |
| `comment` | `true` | Post or update one PR comment |
| `fail-on-machine-problem` | `true` | When *nothing* could be checked (machine problem), fail the job (`true`) or only warn (`false`). Either way it is labelled as a machine problem, never as an app failure |
| `artifact-name` | `everyrole-report` | Name of the uploaded artifact; set a unique one when several jobs use the action |
| `python-version` | `3.12` | Python used to run EveryRole |
| `app` | *(empty)* | Tests kept in EveryRole: your app's name there. The action then runs `everyrole ci` (its tests, with each login from this step's `env`: `EVERYROLE_LOGIN_<KIND>_USERNAME` / `_PASSWORD`) instead of the flows in the repository. See [Run in your own CI](../README.md#run-in-your-own-ci) |
| `api-key` | *(empty)* | With `app`: a workspace API key from EveryRole (`${{ secrets.EVERYROLE_API_KEY }}`) |
| `everyrole-url` | `https://everyrole.dev` | With `app`: EveryRole's address |

## Outputs

| Output | Meaning |
|---|---|
| `report-dir` | Folder with `results.json`, `summary.md`, `junit.xml`, screenshots and videos |
| `result` | `passed`, `failed` (the app failed a check) or `not-checked` (machine problem or setup error) |
| `exit-code` | `0`, `1` or `2` (see [verdicts.md](verdicts.md)) |

## Environment

Pass model settings and every `{{secrets.NAME}}` your flows use as `env` on the action step.

| Variable | Required | Meaning |
|---|---|---|
| `TYPESAFE_API_KEY` | yes | Key for Jev (TypeSafe key, or your Vercel AI Gateway key) |
| `TYPESAFE_URL` | with the gateway | `https://ai-gateway.vercel.sh/typesafe/v1/systemone`; default is TypeSafe directly |
| `TYPESAFE_MODEL` | with the gateway | `typesafe-ai/jev`; default `jev-latest` |
| `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL`, `TEXT_MODEL`, `TEXT_MODEL_REASONING` | no | Text helper, only used when a field needs text none of the named values fits |

Variables passed as empty strings (for example a repository variable nobody defined) count as unset.

## What each step does

1. Sets up Python and installs EveryRole from the action's own folder.
2. Finds Chrome (`CHROME_PATH`, then the usual install locations; GitHub's Ubuntu, Windows and macOS runners
   have one) or installs one with `browser-actions/setup-chrome`, and exports `CHROME_PATH`.
3. On Linux, lifts Ubuntu 24.04's restriction on unprivileged user namespaces so Chrome's sandbox can start,
   and installs ffmpeg if videos are on and it is missing.
4. Runs `everyrole run` and appends `summary.md` to the job summary.
5. Uploads the report folder (results, JUnit, screenshots, videos) as an artifact.
6. Posts or updates the PR comment, with a link to the run for the videos. A comment that cannot be posted
   (for example on a pull request from a fork, whose token is read-only) prints a warning and never hides the
   test result.
7. Sets the job result:
   - exit `1` → `::error` "your app failed a check" and a failed job;
   - exit `2` → "everyrole could not check your app (not an app failure)", as an error (default) or a warning
     with `fail-on-machine-problem: false`.

## Pinning

Pin the action to a release tag (`mkraiemm/everyrole@v0.1.0`) or a commit SHA for reproducible runs. `@v0` follows
the latest 0.x release.
