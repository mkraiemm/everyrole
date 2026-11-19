# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `everyrole ci --app <app>`: run an app's tests kept in EveryRole in your own CI (GitLab, GitHub, any CI),
  with each test login read from masked CI variables that never leave the job; model calls go through
  EveryRole with the API key; results uploaded, written as `junit.xml`, and commented on the merge or pull
  request with `--comment`. GitLab job template: `everyrole/ci_files/everyrole.gitlab-ci.yml`.
- GitHub Action: `app`, `api-key` and `everyrole-url` inputs for the same connected mode.
- Journeys (`steps`), ways in (`way_in`), test importance, flaky tests set aside, new checks (`count`, `kept`,
  `signin_form_shown`, `email_received`), per-user-type sign-in pages and login links. See
  [the reference](docs/flow-spec.md).
- Extras: `everyrole[replays]` (Pillow: the cursor drawn in replays), `everyrole[login-links]`.

### Changed

- `everyrole doctor` makes one tiny call to each configured model, so a model the provider no longer offers
  is found before a run.

## [0.1.0] - 2026-09-28

First public release.

### Added

- `everyrole run`: run plain-English flows against any deployed URL in a throwaway headless Chrome, in parallel,
  with per-flow step and time budgets.
- Apps on several addresses: named parts per environment (`environments`, `{{URL.<part>}}`, `part:/path`
  pages, `--env`, `--part`).
- `everyrole signin --persona NAME`: try one user type's sign-in and see where it landed.
- Sign-ins EveryRole cannot type: security checks (CAPTCHAs) are recognised and reported as "sign in
  yourself", never as an app failure; `saved_signin` reuses a session saved by hand, checked for expiry without
  any AI; the `password_field_absent` check proves a sign-in really finished.
- A run is a good neighbour: memory and CPU caps enforced by the operating system (`run.limits`), a network pace
  shared by every process of the run (`run.network`), page-access checks in parallel batches
  (`run.access_batch`), and no browser-control daemon or tab outliving its run.
- `usage.json` (model calls, tokens and cost as the provider reported them) and `progress.json` (the run's live
  state) in the report folder.
- Flow format and JSON schemas (`everyrole/schema/`), with templates for the deploy URL, a per-run id and
  secrets from the environment.
- User types (`personas`): sign in once per user type, reuse the session in every flow, and run one flow as
  several user types with their own checks (`as`, `expect_denied`).
- Access matrix: who may open which page, checked without any AI call.
- Form-only flows (`block_writes` + `tried_to_save`): submit forms for real with every write blocked.
- Safety guards enforced in code: secret redaction on every model request and report, value names only for
  the model, `forbid` words, `allowed_hosts`, isolated browser contexts, private session storage.
- Honest verdicts: app failure, "not checked" (machine problem), "not run", and "passed on 2nd try"; one
  retry for every unit, with browser healing before retrying a machine problem; a site-down pre-check.
- Reports: PR-comment markdown, `results.json`, JUnit XML, failure screenshots, replay videos (`--videos`).
- `everyrole validate` (offline), `everyrole doctor` (machine readiness, no model calls), `everyrole comment`
  (one pull-request comment, updated in place).
- GitHub Action (`action.yml`) with Chrome and ffmpeg setup, artifacts, PR comment and a distinct result for
  "nothing could be checked".
- Vendored, patched [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) engine (`engine/`, MIT).

[Unreleased]: https://github.com/mkraiemm/everyrole/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mkraiemm/everyrole/releases/tag/v0.1.0
