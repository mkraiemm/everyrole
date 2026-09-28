# Getting started

About 5 minutes: install, check the machine, run the checks that come with the demo app, then write a
check for your own app.

## 1. Install

You need Python 3.12 or newer, Chrome or Chromium, and ffmpeg if you want replay videos.

```bash
pip install git+https://github.com/mkraiemm/everyrole.git
```

From a clone of the repository (to run the demo app below):

```bash
git clone https://github.com/mkraiemm/everyrole
cd everyrole
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install .
```

## 2. Add your model key

EveryRole uses [Jev](https://typesafe.ai), a small model that chooses each click. Create a `.env` file in the
folder you run EveryRole from (copy [`.env.example`](../.env.example)):

```bash
TYPESAFE_API_KEY=...
TYPESAFE_URL=https://ai-gateway.vercel.sh/typesafe/v1/systemone   # with a Vercel AI Gateway key
TYPESAFE_MODEL=typesafe-ai/jev
```

With a TypeSafe key, leave out `TYPESAFE_URL` and `TYPESAFE_MODEL`. Details: [Bring your own AI key](ai-keys.md).

## 3. Check the machine

```bash
everyrole doctor
```

```
ok    Python: 3.13
ok    Chrome: C:\Program Files\Google\Chrome\Application\chrome.exe
ok    ffmpeg (replay videos): C:\...\ffmpeg.EXE
ok    Model keys for running tests: set
ok    Model keys for typing free text (optional): set
ok    Model provider reachable: https://ai-gateway.vercel.sh/typesafe/v1/systemone

Ready to test.
```

`doctor` calls no model. Every line that is not `ok` says how to fix it; see
[Troubleshooting](troubleshooting.md).

## 4. Run the demo

The repository ships a tiny made-up app ("Acme Notes") in `fixtures/` and checks for it in
`examples/quickstart/`. Serve the app in one terminal:

```bash
python examples/quickstart/serve.py 8794
```

In another terminal, add the demo account to your `.env` (the demo app accepts any email and any password of
8 or more characters):

```bash
DEMO_EMAIL=demo@example.com
DEMO_PASSWORD=demo-password-1
```

Check the files, then run them:

```bash
everyrole validate examples/quickstart/flows --config examples/quickstart/everyrole.config.json
everyrole run examples/quickstart/flows --config examples/quickstart/everyrole.config.json \
  --base-url http://127.0.0.1:8794 --videos
```

`validate` is offline: it checks the files against the schemas and resolves every template, with no
browser and no AI. `run` launches its own throwaway headless Chrome, signs in once as the `member` user type,
runs the three flows and the access rules in parallel, prints the pull-request comment, and writes
`everyrole-report/`. See [Replays and reports](replays-and-reports.md).

The run holds itself to half of this computer's memory and CPU and paces its page loads, so it does not
freeze your laptop or flood your network; its first and last lines say what it got and what it used. Change
that with `run.limits` and `run.network` in the config ([reference](flow-spec.md)). To tune a sign-in on its
own, `everyrole signin --persona member` signs in once and says where it landed.

## 5. Your own app

Create `flows/pricing.json`:

```json
{
  "name": "visitor-finds-pricing",
  "start": "{{BASE_URL}}/",
  "goal": "Open the pricing page.",
  "expect": [{ "url_contains": "/pricing" }]
}
```

```bash
everyrole validate flows
everyrole run flows --base-url https://your-app.example.com
```

Next: [Writing flows](writing-flows.md), then put it on every deploy with the
[GitHub Action](github-action.md) or [any other CI](ci.md).
