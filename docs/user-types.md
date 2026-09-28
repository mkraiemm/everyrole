# User types and access rules

Most apps look different for a visitor, a free user, a paying user and an admin. EveryRole tests each of them,
and checks who may open which page without calling any AI.

All of it lives in `everyrole.config.json` (read from the current folder, or `--config PATH`). A complete
fictional example: [`examples/saas-app/everyrole.config.json`](../examples/saas-app/everyrole.config.json).

## Defining user types

```json
{
  "login": {
    "start": "{{BASE_URL}}/login",
    "goal": "Sign in with the account's email and password.",
    "expect": [{ "url_contains": "/dashboard" }],
    "forbid": ["Delete", "Sign out", "Log out"]
  },
  "personas": {
    "admin": { "values": { "email": "{{secrets.ADMIN_EMAIL}}", "password": "{{secrets.ADMIN_PASSWORD}}" } },
    "pro":   { "values": { "email": "{{secrets.PRO_EMAIL}}",   "password": "{{secrets.PRO_PASSWORD}}" } },
    "free":  { "values": { "email": "{{secrets.FREE_EMAIL}}",  "password": "{{secrets.FREE_PASSWORD}}" } },
    "guest": { "login": false }
  }
}
```

- `login` is the sign-in recipe, shared by every user type. It is a small flow: a start page, a goal and
  checks that prove the user is signed in.
- Each user type in `personas` brings its own `values` (usually from `{{secrets.*}}`). `"login": false` means a
  signed-out visitor.
- During `everyrole run`, each user type that is needed signs in **once**, in parallel with the others. Its
  session (cookies and local storage) goes to a private temporary folder and is restored into each of its
  flows before the first page loads. The folder is deleted at the end of the run.
- A sign-in is retried once. If it still fails, that user type's flows and access checks are reported as
  "not run", and the sign-in itself as the failure (with a screenshot and, with `--videos`, a replay).

Use test accounts without two-factor codes, CAPTCHAs or email verification (or see
[Sign-ins EveryRole cannot type](#sign-ins-everyrole-cannot-type)).

### Trying one sign-in

```bash
everyrole signin --persona pro
```

signs in as `pro` exactly as a run does (with `--base-url`, `--env` and `--part` like `everyrole run`), then
prints where it landed, e.g. `signed in, landed on /dashboard`, or why it failed. Exit code 0 when it worked.
`--output result.json` also writes the result as JSON. Nothing else runs, so it is the quickest way to tune the
`login` recipe.

A good `login.expect` proves the sign-in really finished: a signed-in page (`url_contains: "/dashboard"`) and,
for two-step sign-ins (email first, password on a second screen), `{"password_field_absent": true}`, so the
password screen itself never counts as signed in.

### Sign-ins EveryRole cannot type

Some sign-ins cannot be done by a program: a CAPTCHA, a two-factor code, "Continue with Google", a link sent
by email. EveryRole recognises these and never reports them as your app being broken:

- A sign-in or a test that stops on a page with a known security check (Cloudflare Turnstile, Google
  reCAPTCHA, hCaptcha, Arkose Labs, AWS WAF; the list is data in
  [`everyrole/challenges.json`](../everyrole/challenges.json)), or a sign-in that never showed a password
  field, ends as **not checked: sign in yourself**. EveryRole does not try to solve security checks.
- That user type's flows and access checks are then "not run", not failed.

For such a user type, sign in by hand once and give EveryRole the saved session instead of a recipe:

```json
{
  "personas": {
    "admin": { "saved_signin": { "file": "sessions/admin.json", "check": "/admin" } }
  }
}
```

- `file` is a session file, relative to the config: `{"cookies": [...], "storage": {"https://app.example.com":
  [["key", "value"], ...]}}`. Cookies use the Chrome DevTools Protocol format (`Network.getAllCookies`);
  `storage` holds the local storage of each origin. The open-source CLI reads this file but does not record it:
  save it from a browser you signed in with (any tool that can export cookies and local storage in that
  format). It holds live credentials: keep it out of git and out of CI logs.
- Before a run uses it, `check` (a signed-in page) is opened with the session, without any AI. If the app sends
  the browser away, the sign-in has expired: the user type's work is reported as "not checked: sign in again",
  never as an app failure.

## Running a flow as a user type

```json
{ "name": "open-dashboard", "as": "pro", "start": "{{BASE_URL}}/dashboard", "goal": "...", "expect": [...] }
```

To run the same flow as several user types, with different expectations, make `as` an object:

```json
{
  "name": "reports-by-plan",
  "start": "{{BASE_URL}}/dashboard",
  "goal": "Open the reports area and export the monthly report as CSV.",
  "as": {
    "pro":  { "expect": [{ "url_contains": "/reports" }, { "element_visible": "Export CSV" }] },
    "free": { "expect_denied": true,
              "expect": [{ "text_visible": "Upgrade to Pro" }, { "element_absent": "Export CSV" }] }
  },
  "forbid": ["Upgrade now", "Delete", "Sign out"]
}
```

This runs twice, reported as `reports-by-plan@pro` and `reports-by-plan@free`.

`expect_denied: true` says this user type should *not* manage the goal. The model is expected to fail (it
cannot find the export, it runs out of steps), and the run never stops early; the checks decide, and they
should confirm the refusal: the upgrade prompt is shown and the export button is absent.

## The access matrix

```json
{
  "access": {
    "/pricing":          { "allow": ["guest", "free", "pro", "admin"] },
    "/dashboard":        { "allow": ["free", "pro", "admin"], "deny": ["guest"] },
    "/reports":          { "allow": ["pro", "admin"], "deny": ["free", "guest"] },
    "/admin":            { "allow": ["admin"], "deny": ["pro", "free", "guest"] }
  },
  "denied_when": [{ "text_visible": "You don't have access to this page" }],
  "access_settle_s": 5
}
```

For each user type, EveryRole opens every page it has a rule for with that user type's session, in batches of
`run.access_batch` pages (each page in its own tab of one shared browser session, batches in parallel like
tests), and watches each page for `access_settle_s` seconds:

- **denied** when the browser ends up on a different path (the app redirected to `/login`, `/`, `/upgrade`...)
  or a `denied_when` check matches (apps that refuse in place);
- **allowed** otherwise.

A page that could not be read, or whose answer disagrees with its rule, is opened again alone before it is
reported, so a slow page on a busy machine does not look like an open door. No model is called: the matrix
is deterministic and free.

In the report it is a table, one row per page and one column per user type:

```
#### Access matrix (no AI)

| Page | guest |
|---|---|
| `/pricing.html` | ✅ allowed |
| `/app.html` | ✅ denied |
| `/admin.html` | ✅ denied |
```

(Real output of the demo app's rules for `guest`.) A wrong answer shows as `❌ allowed (should deny)` or
`❌ denied (should allow)`, with where the browser landed.

Pages on another address of your app are written `part:/path` (e.g. `site:/pricing`), see
[Apps on several addresses](flow-spec.md#apps-on-several-addresses).

## What the access matrix does not do

It checks what the browser is shown, not your API. A page can redirect a free user while its API still answers
them. Test APIs with your backend tests; use EveryRole for what users see.
