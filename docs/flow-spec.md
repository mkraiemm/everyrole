# Flow and config reference

EveryRole reads two kinds of JSON files:

- **Flow files** (`flows/*.json`): one plain-English check each. Schema: [`everyrole/schema/flow.schema.json`](../everyrole/schema/flow.schema.json).
- **The config file** (`everyrole.config.json`, optional): how to sign in, which user types exist, and who may
  open which page. Schema: [`everyrole/schema/config.schema.json`](../everyrole/schema/config.schema.json).

Add `"$schema"` pointing at the schema file to get completion and validation in your editor. `everyrole validate`
checks every file against the schemas and resolves every template, with no browser and no AI calls.

This page documents every field in both schemas; a test fails if a schema field is missing here.

## Templates

Any string in a flow (and `login` / `personas` in the config) may use:

| Template | Value |
|---|---|
| `{{BASE_URL}}` | The address of the app's main part, without a trailing slash: `--base-url`, or the main part's address in the config's `environments` for `--env` |
| `{{BASE_HOST}}` | Its host name (e.g. `my-app-git-feature.vercel.app`) |
| `{{URL.<part>}}` | The address of another part of the app (see [Apps on several addresses](#apps-on-several-addresses)), e.g. `{{URL.site}}/pricing` |
| `{{RUN_ID}}` | A short id, the same for the whole `everyrole run` (use it to make test data unique) |
| `{{secrets.NAME}}` | Environment variable `NAME` (or `NAME` in `.env`). A missing or empty variable is an error |

Every secret a flow uses is tracked. Its literal value never reaches a model and is replaced by
`[secret:NAME]` in every report (see [security.md](security.md)).

## Flow file

```json
{
  "$schema": "../everyrole/schema/flow.schema.json",
  "name": "member-creates-project",
  "title": "The new-project form submits (nothing is saved)",
  "as": "member",
  "start": "{{BASE_URL}}/app",
  "goal": "Create a new project using the project name value.",
  "values": { "project_name": "Quarterly review {{RUN_ID}}" },
  "block_writes": true,
  "expect": [
    { "text_visible": "Create a project" },
    { "tried_to_save": "/api/projects" }
  ],
  "forbid": ["Delete", "Sign out"],
  "run_on": ["preview"]
}
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `$schema` | string | | Path or URL of the schema, for editors. Ignored when running |
| `name` | string, required | | Stable id: lowercase letters, digits and dashes, up to 64 characters. Used in reports; must be unique across the suite |
| `title` | string | | What the check proves, in one sentence, for people (e.g. "A paying user can create a new project") |
| `start` | string, required | | The http(s) URL the browser opens first. Usually `{{BASE_URL}}/some/path` |
| `goal` | string, required | | The task in plain English. The model reads it to choose each click; it never decides pass/fail |
| `values` | object | `{}` | Named values the model may type, e.g. `{"email": "{{secrets.QA_EMAIL}}"}`. Names: lowercase, digits, `_`. The model only picks *which* name fits a field; EveryRole types the value itself, and secret values are shown to the model only as their name |
| `as` | string or object | | Run as a user type from the config. A string runs once as that user type. An object runs once per key, each with its own checks (see below). Without `as`, the flow runs in a fresh signed-out browser |
| `as.<persona>.expect` | checks | the flow's `expect` | Checks for this user type's run |
| `as.<persona>.expect_denied` | boolean | the flow's `expect_denied` | This user type should not be able to do the goal |
| `expect` | checks | | The only source of pass/fail: every check must hold on the settled page. Required unless every `as` entry has its own |
| `expect_denied` | boolean | `false` | The user should NOT be able to do the goal. The model giving up or running out of steps is then expected; pass/fail still comes only from `expect`, which should confirm the refusal (e.g. `text_visible: "Upgrade to Pro"`) |
| `block_writes` | boolean | `false` | Form-only mode: every request that would change data (POST, PUT, PATCH, DELETE, form posts, beacons) is blocked and recorded. The flow can fill and submit forms without saving anything. Pair it with a `tried_to_save` check |
| `ignore_saves` | list of strings | `[]` | Addresses the page writes to on its own (analytics, metrics). Blocked writes whose URL contains one of these never count for `tried_to_save` |
| `forbid` | list of strings | `[]` | Words or phrases EveryRole refuses to click or type into (whole word, case-insensitive: `Pay` blocks "Pay now", not "Payment history"). Enforced in code before every action; hitting one ends the flow as `forbidden_action` |
| `allowed_hosts` | list of strings | the app's hosts | Hosts the flow may visit (subdomains included). By default: every part of the app plus the start URL's host. Navigating anywhere else ends the flow as `left_allowed_hosts` |
| `max_steps` | integer 1-60 | `25` | Most actions (clicks, typing) the flow may take |
| `timeout_s` | integer 5-600 | `90` | Wall-clock budget for the flow, in seconds |
| `settle_s` | integer 0-30 | `5` | After the model stops (or the checks first pass), keep re-checking for up to this many seconds so redirects and slow rendering can finish. Text that only exists while loading never passes a flow |
| `submit_settle_s` | integer 0-60 | `15` | With `block_writes`: how long to wait for the page to go quiet before judging. Apps often validate input for a while before they try to save |
| `scroll_retries` | integer 0-10 | `3` | When the model gives up but the page continues below the fold, scroll one screen and let it try again, up to this many times |
| `run_on` | list of strings | `[]` (always) | Environments this flow runs in, matched against `everyrole run --env`. Use `["preview"]` for anything that changes real data |
| `steps` | list | | A journey: steps done one after the other in the same browser, each judged by its own checks (`goal`, `expect`, ...) before the next starts. The first step that fails stops the journey; pass/fail still comes only from checks |
| `way_in` | object | | Per user type (`""` for the visitor): the page it starts on and the links it clicks to reach this test's page (`{"entry": "/", "clicks": [{"label": "Pricing", "to": "/pricing"}]}`). The test then runs as those clicks, then its own goal and checks: it proves people can get there, not only that the page opens |
| `importance` | `critical`, `important` or `minor` | `important` | How much this test matters: critical = money and access (sign up, log in, pay, the core action), important = daily use, minor = content (blog, legal, docs). Critical tests are listed first and counted apart in the summary |
| `importance_why` | string | | One line: why this level |
| `importance_set_by` | string | | Set when a person chose the level, so tools that write tests never change it |
| `changes_password` | boolean | `false` | The test changes its user type's own password for real: it types the new password from `EVERYROLE_NEW_PASSWORD_<USER TYPE>`, then the run signs in with it. Not run where no new password was prepared (a CI run) |
| `deletes` | boolean | | The journey presses Delete or Remove account: it destroys what it uses, so it runs only when asked (`everyrole run --skip-delete` leaves it out) |
| `account` | string | | The email of the run's test account this journey uses, when a run made several for one user type. Written by EveryRole |
| `measured_ms` | integer | | How long the journey took before it ran as a test, used until it has run times of its own. Written by EveryRole |
| `found_on` | string | | The environment this test was found on, when it wasn't production (e.g. `staging`). On production it is tried once at the next run and kept when it works there |
| `promoted_from` | string | | Set when a test found on another environment worked on production and now runs everywhere |
| `source` | object | | Where the test came from, when it was kept from a suggestion. Written by EveryRole; ignored when running |
| `source.exploration` | integer | | The exploration that suggested it |
| `source.at` | string | | When that exploration ended (ISO 8601) |
| `release` | object | | A journey kept as a test that waits for a release to prove it on production. Written by EveryRole |
| `release.change` | integer | | The change that brought this test |
| `release.journey` | string | | Its journey's name in that change |
| `release.live` | boolean | `false` | Proven on production: until then it runs on staging and previews only |

### Checks

A check is an object with exactly one key. All checks in a list must pass. Text comparisons ignore case and
collapse whitespace.

| Check | Passes when |
|---|---|
| `url_contains` | The current URL contains this text |
| `url_not_contains` | The current URL does not contain this text |
| `url_matches` | The current URL matches this regular expression |
| `text_visible` | The page's rendered text contains this text |
| `text_absent` | The page's rendered text does not contain this text |
| `element_visible` | A visible control (link, button, field, tab) has a label, placeholder or title containing this text |
| `element_absent` | No visible control has such a label |
| `tried_to_save` | With `block_writes`: the page tried to write to an address containing this text (or to any address for `"*"`). Beacons and `ignore_saves` addresses never count |
| `password_field_absent` | `true` only. The page shows no password field. Useful in `login.expect`: a two-step sign-in's password screen, or a sign-in form shown again after a refused attempt, still has one |
| `count` | The text appears this many times: `{"text": "Your order", "equals": 1}` (also `at_least`, `at_most`). Proves something happened once and only once, especially after a reload |
| `kept` | `true` only. Everything the journey typed in its earlier steps is still on the page (nothing lost after signing up or logging in partway) |
| `signin_form_shown` | The address matches this regular expression (a sign-in page) and the page asks for a password: signed out, told from the page itself |
| `email_received` | An email to the journey's new account arrived whose subject or text contains this (a welcome or confirmation email). Needs EveryRole's inboxes |

### One flow, several user types

```json
{
  "name": "reports-by-plan",
  "start": "{{BASE_URL}}/dashboard",
  "goal": "Open the reports area and export the monthly report as CSV.",
  "as": {
    "pro":  { "expect": [{ "element_visible": "Export CSV" }] },
    "free": { "expect_denied": true, "expect": [{ "text_visible": "Upgrade to Pro" }] }
  }
}
```

This runs twice, reported as `reports-by-plan@pro` and `reports-by-plan@free`. Each run starts from that
user type's saved session. Values from the user type (`personas.<persona>.values`) are available to the
flow, and the flow's own `values` win on a name clash.

### Result statuses

Each flow ends with one status. Only the checks decide `passed`; the model saying "done" never does.

| Status | Meaning | Counts as |
|---|---|---|
| `passed` | Every check held on the settled page | passed |
| `failed_expect` | The model finished but a check did not hold | app failure |
| `blocked` | The model gave up (the button or page it needed was not there) and the checks did not hold | app failure |
| `budget_exceeded` | `max_steps` or `timeout_s` ran out | app failure |
| `forbidden_action` | The flow tried to act on a `forbid` word | app failure |
| `left_allowed_hosts` | The flow navigated outside `allowed_hosts` | app failure |
| `error` | Chrome, the model provider or a worker failed | not checked (machine problem) |

See [verdicts.md](verdicts.md) for how these become passed / failed / not checked / not run.

## Config file

```json
{
  "$schema": "../everyrole/schema/config.schema.json",
  "login": {
    "start": "{{BASE_URL}}/login",
    "goal": "Sign in with the account's email and password.",
    "expect": [{ "url_contains": "/dashboard" }],
    "forbid": ["Delete", "Sign out"]
  },
  "personas": {
    "admin": { "values": { "email": "{{secrets.ADMIN_EMAIL}}", "password": "{{secrets.ADMIN_PASSWORD}}" } },
    "free":  { "values": { "email": "{{secrets.FREE_EMAIL}}",  "password": "{{secrets.FREE_PASSWORD}}" } },
    "guest": { "login": false }
  },
  "access": {
    "/dashboard": { "allow": ["free", "admin"], "deny": ["guest"] },
    "/admin":     { "allow": ["admin"], "deny": ["free", "guest"] }
  },
  "denied_when": [{ "text_visible": "You don't have access" }]
}
```

`everyrole` reads `./everyrole.config.json` when it exists, or the file given with `--config`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `$schema` | string | | Path or URL of the schema, for editors |
| `login` | object | | How a user type signs in. Required when any user type signs in. It runs once per user type per suite; the signed-in session (cookies and local storage) is reused by every flow of that user type |
| `login.start` | string, required | | Sign-in page URL |
| `login.goal` | string, required | | The sign-in task in plain English. The user type's `values` (email, password) are available |
| `login.expect` | checks, required | | Proof of being signed in (e.g. `url_contains: "/dashboard"`) |
| `login.forbid` | list of strings | `[]` | Words the sign-in must never act on |
| `login.max_steps` | integer 1-60 | `12` | Step budget for signing in |
| `login.timeout_s` | integer 5-600 | `60` | Time budget for signing in, in seconds |
| `login.settle_s` | integer 0-30 | `5` | Re-check window after signing in (redirects) |
| `personas` | object | `{}` | User types, by name (lowercase letters, digits, `_`, `-`) |
| `personas.<persona>.values` | object | `{}` | Named values this user type types, usually `email` and `password` from `{{secrets.*}}` |
| `personas.<persona>.login` | boolean | `true` | `false` = a signed-out visitor (no sign-in, fresh browser) |
| `personas.<persona>.start_paths` | list of pages | `[]` | Where this user type's own area starts (e.g. `/admin`). Read by EveryRole's exploration tools (not part of this package); `everyrole run` ignores it |
| `personas.<persona>.saved_signin` | object | | Reuse a sign-in a person did by hand (e.g. to pass a CAPTCHA) instead of typing a password. The user type then needs no `login` recipe. See [User types](user-types.md#sign-ins-everyrole-cannot-type) |
| `personas.<persona>.saved_signin.file` | string, required | | The session file (cookies and local storage, JSON), relative to the config file |
| `personas.<persona>.saved_signin.check` | page | `/` | A signed-in page. Before a run uses the session, this page is opened with it (no AI): if the app sends the browser away (or a `denied_when` check matches), the sign-in has expired and the user type's work is reported as "not checked: sign in again", never as an app failure |
| `personas.<persona>.saved_signin.saved_at` | number | | When the session was saved (Unix time). Informational |
| `personas.<persona>.signin` | object | | Where this user type signs in when it is not `login.start`, and how to tell it is signed in there. The rest of the recipe (goal, limits) comes from `login` |
| `personas.<persona>.signin.start` | string | | This user type's own sign-in page (e.g. `{{URL.admin}}/login`) |
| `personas.<persona>.signin.expect` | checks | `login.expect` | Proof of being signed in there |
| `personas.<persona>.signin.goal` | string | `login.goal` | Only when this user type's sign-in is not the usual one (e.g. accepting an emailed invitation): what to do there instead |
| `personas.<persona>.signin_by` | string | `""` | How this user type signs in, when known: `"email"` (a link the app emails) or `"password"` |
| `personas.<persona>.login_link` | object | | Signed in by the app itself, with no password: EveryRole POSTs a request signed with the `EVERYROLE_LOGIN_LINK_KEY` secret (an Ed25519 private key) to the app's endpoint and opens the one-time sign-in link it answers with. Needs `pip install "everyrole[login-links]"` |
| `personas.<persona>.login_link.url` | string | | The app's login-link endpoint |
| `personas.<persona>.login_link.check` | string | where the link landed | A signed-in page that proves the sign-in |
| `personas.<persona>.production` | boolean | `true` | `false`: this user type is never tested on production (an admin, until the team allows it there); other environments are unaffected |
| `access` | object | `{}` | The access matrix: page (`/path`, or `part:/path` on another part of the app) to who may and may not open it. Checked with no AI |
| `access.<path>.allow` | list of user types | `[]` | These user types must be able to open the page |
| `access.<path>.deny` | list of user types | `[]` | These user types must be refused |
| `denied_when` | checks | | Extra refusal signals, for apps that refuse in place (e.g. a "no access" message) instead of redirecting. Being sent to a different page than the one requested always counts as a refusal |
| `access_settle_s` | integer 0-30 | `5` | How long to watch each page for a redirect before deciding it was allowed |
| `environments` | object | | Where the app lives in each environment: environment name to `{part: address}`. See [Apps on several addresses](#apps-on-several-addresses). Without it, the app is one address given with `--base-url` |
| `run` | object | `{}` | How a run uses this computer and its network (below). Every field has a default |
| `run.parallel` | integer 1-8 | `3` | Tests, sign-ins and page-access checks running at the same time. `everyrole run --parallel` overrides it |
| `run.access_batch` | integer 1-50 | `8` | Pages one page-access check opens at once, each in its own tab. A user type with more pages gets several checks, run in parallel like tests |
| `run.limits` | object | `{}` | How much of this computer a run may use. The operating system holds the whole run (every worker and every Chrome) to it: a job object on Windows, a cgroup on Linux. New work waits for room instead of piling up |
| `run.limits.memory_percent` | number 5-95 | `50` | Share of this computer's memory (or of its container's) for the run |
| `run.limits.cpu_percent` | number 5-100 | `50` | Share of all this computer's cores for the run |
| `run.network` | object | `{}` | How hard a run may use the network it runs on. Pages start as fast as they finish (the recent median load time divided by `pages_at_once`), within the gap limits, so a slow network slows the run down instead of the run taking the network down |
| `run.network.pages_at_once` | integer 1-20 | `3` | Pages loading at the same time in the whole run (every worker, tab and user type together) |
| `run.network.min_gap_s` | number 0-60 | `1` | Shortest wait between two new pages, however fast the network is |
| `run.network.max_gap_s` | number 1-120 | `15` | Longest wait between two new pages, however slow the network gets |
| `run.network.page_timeout_s` | number 10-600 | `60` | A page still loading after this long gives its place back |
| `run.flaky_runs` | integer 1-100 | `10` | How many of a test's recent runs (same environment, same user type) are looked at to tell whether it is flaky: failed, then passed again. A flaky test is set aside, never shown as broken |
| `run.flaky_clears_after` | integer 1-50 | `3` | Passes in a row (each on the first try) after which a flaky test is trusted again |
| `discover` | object | `{}` | Settings for EveryRole's exploration tools (not part of this package). Accepted so one config file works everywhere; `everyrole run` and `everyrole validate` ignore it |
| `discover.start_paths` | list of pages | `["/"]` | Where exploration starts |
| `discover.max_pages` | integer 1-200 | `20` | Pages visited per user type |
| `discover.max_depth` | integer 0-5 | `3` | Link hops from a start page |
| `discover.forbid` | list of strings | `[]` | Extra words exploration never follows or clicks |
| `discover.max_proposals` | integer 1-30 | `8` | Tests exploration may propose |
| `discover.author_parallel` | integer 1-16 | `4` | Drafting calls to the text model at the same time |
| `discover.author_timeout_s` | integer 30-900 | `240` | How long one drafting call may take |
| `discover.page_settle_s` | integer 1-30 | `8` | Longest wait for a page to finish loading before it is read |
| `discover.probe` | boolean | `true` | Click buttons (with writes blocked) to see what they open |
| `discover.max_probes` | integer 0-200 | `25` | Buttons clicked per user type |
| `discover.weigh_ahead` | integer 1-50 | `8` | Pages weighed before choosing which to open next (lightest first) |

### How the access matrix decides

For each user type with a rule, EveryRole opens the listed pages in batches of `run.access_batch`, each page in
its own tab of one shared browser session (restoring that user type's session), and watches each for
`access_settle_s` seconds. Batches run in parallel like tests:

- **denied** if the browser ends up on a different path than the one requested (sent to `/login`, `/`,
  `/upgrade`...) or a `denied_when` check matches;
- **allowed** otherwise.

A page that could not be read, or whose answer disagrees with the rule, is checked again alone before it is
reported. No model is called, so the matrix costs nothing and is deterministic.

## Apps on several addresses

Many apps live on more than one address: a public site on `example.com`, the app itself on `app.example.com`.
Each address is a *part* with a name; the first part listed is the main one. Declare them per environment:

```json
{
  "environments": {
    "production": { "app": "https://app.example.com", "site": "https://example.com" },
    "staging":    { "app": "https://staging-app.example.com", "site": "https://staging.example.com" }
  },
  "access": {
    "/dashboard":    { "allow": ["member"], "deny": ["guest"] },
    "site:/pricing": { "allow": ["member", "guest"] }
  }
}
```

- Pages of the main part are plain paths (`/dashboard`); pages of another part are written `part:/path`
  (`site:/pricing`) in `access`, `saved_signin.check` and `start_paths`.
- Flows start on another part with `{{URL.<part>}}` (`"start": "{{URL.site}}/pricing"`). A flow's default
  `allowed_hosts` covers every part of the app.
- Every environment lists the same parts, in the same order.
- `everyrole run --env staging` tests the staging addresses. `--base-url` replaces the main part's address (a
  preview deploy) and `--part site=https://...` any other part's.

A config without `environments` is a one-part app: pass its address with `--base-url`.
