# Quickstart: test a demo app on your machine

A tiny made-up app ("Acme Notes") lives in [`fixtures/`](../../fixtures): a home page, a pricing page, a
sign-in page, a projects page for signed-in users and an admin page nobody may open. This folder tests it:

| File | What it checks |
|---|---|
| `flows/public-pricing.json` | a visitor finds the plans from the home page |
| `flows/member-opens-project.json` | a signed-in member opens one of their projects |
| `flows/member-creates-project.json` | the new-project form submits, with saving blocked (nothing is saved) |
| `everyrole.config.json` | how to sign in, the `member` and `guest` user types, and who may open which page |

## Run it

From the repository root, in two terminals:

```bash
# 1. serve the demo app on http://127.0.0.1:8794 (plain python -m http.server, bound to localhost)
python examples/quickstart/serve.py 8794
```

```bash
# 2. add to your .env: TYPESAFE_API_KEY (+ URL/model, see .env.example) and the demo account
#    DEMO_EMAIL=demo@example.com
#    DEMO_PASSWORD=any-8-characters      (the demo app accepts any email and 8+ character password)
everyrole doctor
everyrole validate examples/quickstart/flows --config examples/quickstart/everyrole.config.json
everyrole run examples/quickstart/flows --config examples/quickstart/everyrole.config.json \
  --base-url http://127.0.0.1:8794 --videos
```

EveryRole launches its own throwaway headless Chrome, signs in once as `member`, runs the three flows and the
access matrix in parallel, and writes `everyrole-report/`:

```
everyrole-report/
  summary.md      the pull-request comment (also printed to the terminal)
  results.json    every check, step and timing
  junit.xml       for CI test views
  videos/*.mp4    a replay of each flow (with --videos and ffmpeg installed)
```

The demo server answers `501` to the page's own analytics POST: that is expected and harmless.
