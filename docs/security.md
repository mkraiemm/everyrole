# Security model

EveryRole signs into your app with real test accounts and clicks things. The rules below are enforced in code,
not by asking a model to behave. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What leaves your machine

EveryRole talks to exactly three places: **your app** (the browser), **the Jev model provider** you configure
(`TYPESAFE_URL`), and, only when a flow must type text none of its named values fits, **the text model**
you configure (`TEXT_MODEL_BASE_URL`). With `everyrole comment`, the report also goes to the GitHub API.

What a model receives per decision: the goal, the page's URL, title and text, the list of controls on it
(labels, roles, current values), the recent actions, and the *names* of the values it may type. No
screenshots are sent: the frames recorded for replay videos stay on your machine.

### Secrets never reach a model

1. **Value names only.** A flow's `values` are typed by EveryRole itself. The model picks *which* named value
   fits a field ("password"); secret values are described to it as "the secret value named 'password' (its
   content is hidden)" and the literal is substituted locally, after the decision.
2. **One outbound filter.** Every request body to any model passes through a single choke point in the
   engine (`OUTBOUND_FILTERS`, patch P3 in [engine/PATCHES.md](../engine/PATCHES.md)). EveryRole registers a
   redaction filter there that replaces every secret literal (raw, and as it appears inside JSON) with
   `[secret:NAME]`. This covers a typed password echoed back into page text or the action history.
3. **Password fields are opaque.** The page snapshot reports a password input as `[filled]` or empty, never
   its contents (patch P4).
4. **Reports are scrubbed.** `results.json`, `summary.md`, `junit.xml`, the PR comment and the terminal
   output go through the same redactor before they are written.

A "secret" is any value that came from `{{secrets.NAME}}`. Keep credentials in environment variables or a
local `.env` file (git-ignored), never in flow files.

### Sessions

Each user type signs in once per run. Its session (cookies and local storage) is saved in a private
temporary folder (file mode `600` where the OS supports it), restored into that user type's flows, and
deleted when the run ends. Session files are never written to the report folder.

A saved sign-in (`personas.<persona>.saved_signin`) is a session file you keep yourself. EveryRole only reads
it; treat it like a password (git-ignored, never printed in CI logs), and replace it when it expires.

## What EveryRole will not do to your app

### Write-guard (form-only flows)

With `"block_writes": true`, a script installed before the page's own scripts blocks every request that
would change data and records it instead:

- `fetch` and `XMLHttpRequest` with any method other than GET, HEAD or OPTIONS,
- form submissions that are not GET,
- `navigator.sendBeacon`.

The flow can fill in and submit a form for real; the save request is stopped, and a `tried_to_save` check
proves the submit fired. Nothing is created, sent or charged.

**Limits.** The guard runs inside the page; it is not a network proxy. It does not intercept requests made
by service workers, WebSocket messages, native form posts to another origin, or apps that change data on a
plain page load (GET). For those, use the two guards below and run data-changing flows on preview deploys
only (`"run_on": ["preview"]`).

### Forbid words

`forbid` (per flow, and `login.forbid`) lists words EveryRole refuses to act on: before every click or keystroke
the target's label is checked (whole word, case-insensitive: `Pay` blocks "Pay now", not "Display" or
"Payment history"). A hit stops the flow as `forbidden_action` before any input reaches the browser. Put
`Delete`, `Remove`, `Pay`, `Buy`, `Subscribe`, `Send`, `Sign out` in every flow that does not need them.

### Allowed hosts

A flow may only visit the app's hosts (every part of the app, see
[Apps on several addresses](flow-spec.md#apps-on-several-addresses)) and its start URL's host, with their
subdomains, or the hosts listed in `allowed_hosts`.
Navigating anywhere else (a payment provider, an OAuth screen, an external link) ends the flow as
`left_allowed_hosts`.

### Isolation

Every flow runs in its own browser context (like a private window) in a throwaway headless Chrome with a
fresh profile, deleted after the run. Cookies never leak between flows or user types.

## Prompt injection

Page text is untrusted. The model is told so, but that is not what keeps you safe: the model can only
choose among the actions EveryRole offers on the page, every action passes the forbid and host guards first,
it never sees secret values, and it cannot mark a flow as passed.

## Recommendations

- Use dedicated test accounts with the least privilege that the flows need, on a staging or preview
  environment when possible.
- Store credentials as CI secrets; pass them to the action as `env`.
- Run flows that really change data (`block_writes` off) only with `"run_on": ["preview"]`.
- Keep the report artifact private: screenshots and videos show your app's pages as the test accounts see
  them.
