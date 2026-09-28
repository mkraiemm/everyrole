# Writing flows

A flow is one JSON file: where to start, what a user wants to do (in plain English), and how to tell it
worked. This tutorial builds three flows for the demo app in `fixtures/`, each harder than the last. Serve the
app to follow along:

```bash
python examples/quickstart/serve.py 8794
```

The finished files are in [`examples/quickstart/`](../examples/quickstart).

## How a flow runs

1. EveryRole opens `start` in a fresh browser context.
2. The model reads the page (its text and the list of controls) and chooses one action: click this, type into
   that, or stop. EveryRole checks the action against your safety rules, then performs it.
3. After every action, EveryRole reads the page itself and evaluates your `expect` checks. When they all hold on
   a page that has finished loading, the flow passes.
4. If the model stops, runs out of steps or time, or breaks a safety rule, the checks are read one last time
   and decide the result.

The model never decides pass or fail. Write the `goal` for the model and the `expect` checks for yourself.

## Example 1: a public page

A visitor should be able to get from the home page to the plans.

```json
{
  "$schema": "../../../everyrole/schema/flow.schema.json",
  "name": "public-pricing",
  "title": "A visitor can find the plans from the home page",
  "start": "{{BASE_URL}}/index.html",
  "goal": "Open the pricing page.",
  "expect": [
    { "url_contains": "/pricing.html" },
    { "text_visible": "Team" },
    { "text_visible": "$12 per user per month" }
  ],
  "max_steps": 6,
  "timeout_s": 60
}
```

- `name` is the stable id in reports (lowercase, digits, dashes). `title` is the sentence people read.
- `{{BASE_URL}}` is replaced by `--base-url`, so the same file tests localhost, a preview and production.
- Checks are specific enough to fail when the page is wrong: the price is there, not just "some page loaded".
- `max_steps` and `timeout_s` are budgets. A path that suddenly needs 10 clicks fails, which is usually a
  real regression.

**Good checks** look at what a user would notice: the URL, a heading, a price, a button label. Text matching
ignores case and extra whitespace. Avoid text that changes on every visit (dates, counters).

## Example 2: a signed-in user action

A signed-in member should be able to open one of their projects. First describe how to sign in and who the
member is, once, in `everyrole.config.json`:

```json
{
  "login": {
    "start": "{{BASE_URL}}/login.html",
    "goal": "Sign in with the account's email and password.",
    "expect": [
      { "url_contains": "/app.html" },
      { "text_visible": "Signed in as" }
    ],
    "forbid": ["Delete", "Sign out"]
  },
  "personas": {
    "member": {
      "values": { "email": "{{secrets.DEMO_EMAIL}}", "password": "{{secrets.DEMO_PASSWORD}}" }
    }
  }
}
```

Then the flow only says who it runs as:

```json
{
  "name": "member-opens-project",
  "title": "A signed-in member can open one of their projects",
  "as": "member",
  "start": "{{BASE_URL}}/app.html",
  "goal": "Open the project called Launch plan.",
  "expect": [
    { "url_contains": "#/projects/launch-plan" },
    { "text_visible": "Opened project: Launch plan" }
  ],
  "forbid": ["Delete", "Sign out"],
  "max_steps": 6,
  "timeout_s": 60
}
```

- The member signs in **once per run**. The session (cookies and local storage) is restored into every flow
  that runs `as` the member, before the first page loads.
- `{{secrets.DEMO_EMAIL}}` reads the environment variable `DEMO_EMAIL` (or `.env`). The model never sees the
  value: it chooses the *name* `email` for the email field, and EveryRole types the value.
- `forbid` lists words EveryRole must never click. "Delete account" and "Sign out" are on this page, and a flow
  that clicks either stops as `forbidden_action` before anything happens.

## Example 3: a form, tested without saving anything

The "New project" form should work, but a test run must not create projects. `block_writes` blocks every
request that would change data; `tried_to_save` proves the form really tried to save.

```json
{
  "name": "member-creates-project",
  "title": "The new-project form submits (nothing is saved)",
  "as": "member",
  "start": "{{BASE_URL}}/app.html",
  "goal": "Create a new project using the project name value.",
  "values": { "project_name": "Quarterly review {{RUN_ID}}" },
  "block_writes": true,
  "ignore_saves": ["/api/metrics"],
  "expect": [
    { "text_visible": "Create a project" },
    { "tried_to_save": "/api/projects" }
  ],
  "forbid": ["Delete", "Sign out"],
  "max_steps": 10,
  "timeout_s": 90
}
```

- `values` adds `project_name` to the member's `email` and `password`. The model picks `project_name` for the
  "Project name" field. Give flows every value they need: then no free text is ever written by a model.
- `{{RUN_ID}}` is unique per run, handy for data that must not collide.
- The page posts to `/api/metrics` on every load (analytics). `ignore_saves` keeps that from counting as the
  form's save.
- `tried_to_save: "/api/projects"` passes only if submitting the form sent a write to that address. It was
  blocked, so nothing was created; the check proves the button and the form work up to the server.

## Run and iterate

```bash
everyrole validate examples/quickstart/flows --config examples/quickstart/everyrole.config.json
everyrole run examples/quickstart/flows --config examples/quickstart/everyrole.config.json \
  --base-url http://127.0.0.1:8794 --videos
```

When a flow fails, open its replay in `everyrole-report/videos/`: you see every step and the page the checks were
judged on. If the app is right and the flow is wrong, fix the flow:

| Symptom | Change |
|---|---|
| The model clicks around and gives up | Make the `goal` concrete: name the link or button ("Open the Reports tab, then click Export CSV") |
| A check fails on a page that looks right | Use text that is really on the final page; `element_visible` matches control labels, `text_visible` page text |
| Passes locally, fails on slow previews | Raise `settle_s` (re-check window) or `timeout_s` |
| The button is far down the page | Leave `scroll_retries` on (default 3) |

Every field: [Flow and config reference](flow-spec.md). Several user types and who may open which page:
[User types and access rules](user-types.md).
