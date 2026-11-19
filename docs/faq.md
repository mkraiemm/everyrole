# FAQ

**Is it free?**
The CLI, the flow format and the GitHub Action are MIT-licensed. You pay your model provider for the calls your
flows make (see [Bring your own AI key](ai-keys.md)); the access matrix and `validate` make none, and `doctor` one tiny call per model.

**Does EveryRole read my source code?**
No. It only uses your app the way a user does, through a browser, at the URL you give it.

**Does anything go to a EveryRole server?**
No. The CLI talks to your app, to the model provider you configure and, for `everyrole comment`, to the GitHub
API. It sends no telemetry. (Its browser-driving dependency, browser-harness, has opt-out telemetry in its own
`browser-harness` command; EveryRole does not use that command.)

**How is this different from Playwright or Cypress?**
You write what a user wants to do and how to tell it worked, not selectors and click scripts. When the markup,
layout or styling changes, there is no selector to break: the model picks the control from what the page
shows, and your checks still decide. The trade-offs: each step asks a model (slower than a
script, and not free), and it can only do what a user could do in a browser. A sensible split: keep precise
component and API tests in code, and use EveryRole for "does the product still work for each kind of user" after
every deploy.

**Can the AI mark a broken test as passed?**
No. Pass or fail comes only from your `expect` checks, evaluated by EveryRole on the page after it has finished
loading. The model saying "done" is ignored.

**What if the model makes a bad click?**
Every action is checked first: `forbid` words are refused, leaving your app's host ends the flow, and with
`block_writes` no request can change data. A bad click can make a flow fail, and the replay shows it.

**Why did my flow pass "on 2nd try"?**
The first try did not pass (a slow page, a hiccup) and the second did. It counts as a pass so it does not block
your pull request, and the warning says what happened the first time, so flaky checks stay visible.

**What does "not checked" mean?**
EveryRole could not check (Chrome, the model provider or the machine had a problem). It says nothing about your
app and never turns a pull request red on its own. See [Honest results](verdicts.md).

**Can it test logged-in areas?**
Yes: define user types in `everyrole.config.json` with test accounts. Each signs in once per run. Two-factor
codes, CAPTCHAs and email verification are not supported.

**Can it test that a form works without creating data?**
Yes: `"block_writes": true` with a `tried_to_save` check. The form is submitted for real, the save request is
blocked, and the check proves it was sent. See [Writing flows](writing-flows.md#example-3-a-form-tested-without-saving-anything).

**Which browsers?**
Chrome, Chromium or Edge (anything that speaks the Chrome DevTools Protocol), headless by default. Firefox and
Safari are not supported.

**Does it work with single-page apps, React, Vue, Next.js...?**
Yes. EveryRole waits until the page stops loading (no requests in flight, content stable) before it decides or
judges, and restores sessions from cookies and local storage.

**Can I see what happened?**
Run with `--videos` for a replay of every flow at real speed, ending on the page the checks were judged on.
Failed flows also get a screenshot and their list of steps in the report.

**Can it write the flows for me?**
Not in the open-source CLI. Exploring an app and drafting flows for you to review is part of EveryRole's hosted
product, not of this package; the flows it drafts use this same format and run with this CLI.

**Would my flows notice a real bug?**
Plant one and see: `everyrole run flows --base-url URL --inject bug.json` applies the bug to every page before
the app's own scripts run, without touching your app. `bug.json` is `{"rules": [...]}` with rules such as
`{"hide": "Export CSV"}`, `{"disable": "Save"}`, `{"swallow_click": "Create project"}`,
`{"replace_text": ["$12", "$21"]}` or `{"fail_request": "/api/projects", "status": 500}` (all listed in
[`everyrole/inject.py`](../everyrole/inject.py)). A good suite fails on each.

**Where do I report a security problem?**
Privately, see [SECURITY.md](../SECURITY.md).
