# Running in CI

EveryRole is a command-line tool, so it runs in any CI that has Python 3.12+ and Chrome or Chromium. For GitHub,
use the ready-made [GitHub Action](github-action.md). This page covers GitLab and any other CI.

## What every CI setup needs

1. Python 3.12 or newer and `pip install git+https://github.com/mkraiemm/everyrole.git`.
2. Chrome or Chromium. Set `CHROME_PATH` if it is not on `PATH` under a usual name
   (`google-chrome`, `chromium`, `chromium-browser`...). As root (typical in containers), EveryRole starts Chrome
   with `--no-sandbox` automatically.
3. ffmpeg, only for replay videos (`--videos`).
4. `TYPESAFE_API_KEY` (and `TYPESAFE_URL` / `TYPESAFE_MODEL` for the Vercel AI Gateway) plus every
   `{{secrets.NAME}}` your flows use, as masked CI variables.
5. The URL to test, passed with `--base-url`.

Then:

```bash
everyrole run flows --base-url "$DEPLOY_URL" --env preview --output everyrole-report --videos
```

and act on the exit code:

| Exit code | Meaning | Suggested CI behaviour |
|---|---|---|
| `0` | Nothing broken found | pass |
| `1` | At least one app failure | fail the job |
| `2` | Nothing could be checked (machine problem), or EveryRole could not start (invalid flow, no Chrome, no key) | fail or warn, labelled as a machine problem, not an app failure |

Keep `everyrole-report/` as an artifact; publish `everyrole-report/junit.xml` to your CI's test view.

A run takes at most half of the runner's memory and CPU by default (`run.limits`): a job object on Windows, a
cgroup on Linux (created directly as root, else through a systemd user scope). In a container whose cgroup is
read-only, the container's own limits are the caps. On a runner dedicated to testing, raise the shares in
`everyrole.config.json`.

## GitLab CI

```yaml
# .gitlab-ci.yml
everyrole:
  stage: test
  image: python:3.12-slim
  variables:
    CHROME_PATH: /usr/bin/chromium
    PYTHONUTF8: "1"
  before_script:
    - apt-get update -qq && apt-get install -y -qq chromium ffmpeg > /dev/null
    - pip install --quiet everyrole
  script:
    # DEPLOY_URL: your review app / preview URL, e.g. $CI_ENVIRONMENT_URL in a job with an environment
    - everyrole run flows --base-url "$DEPLOY_URL" --env preview --output everyrole-report --videos
  artifacts:
    when: always
    paths:
      - everyrole-report/
    reports:
      junit: everyrole-report/junit.xml
```

Define `TYPESAFE_API_KEY` and your test-account secrets under Settings → CI/CD → Variables (masked).
`everyrole comment` posts to GitHub pull requests only; on GitLab, the JUnit report shows the results in the merge
request, and `everyrole-report/summary.md` can be posted with your own step.

## Tests kept in EveryRole

When your tests live in EveryRole, `everyrole ci --app <your app>` runs them in the CI job, with each test
login read from masked CI variables that never leave the job. It runs in the `test` stage, so it works when it is the only job: GitLab starts no pipeline whose only jobs
are in `.pre` or `.post`. If your pipeline has other jobs (a deploy job), change it to `stage: .post` so the
tests run last, after your deploy; if your file lists its own `stages:` without `test`, use one of yours. See
[Run in your own CI](../README.md#run-in-your-own-ci) and [everyrole.dev/docs#ci](https://everyrole.dev/docs#ci).

## Any other CI (CircleCI, Jenkins, Buildkite, Azure Pipelines...)

The same four steps in a shell:

```bash
python -m pip install git+https://github.com/mkraiemm/everyrole.git
everyrole doctor                     # fails fast, with the fix, if Chrome or a key is missing
everyrole validate flows             # offline check of the flow files
everyrole run flows --base-url "$DEPLOY_URL" --output everyrole-report --videos
```

`everyrole doctor` exits `1` when something required is missing, so it can gate the run.

## Running only some flows per environment

`run_on` in a flow lists the environments it runs in; `--env` picks the environment:

```json
{ "name": "invite-teammate-form", "run_on": ["preview"], "...": "..." }
```

```bash
everyrole run flows --base-url "$PREVIEW_URL" --env preview       # runs it
everyrole run flows --base-url "$PRODUCTION_URL" --env production # skips it
```

Flows without `run_on` run everywhere. Keep anything that really changes data on `preview`.
