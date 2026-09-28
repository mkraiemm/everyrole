# Bring your own AI key

EveryRole calls models directly from your machine or CI, with your own keys. There is no EveryRole server in
between, and the open-source CLI has no account, telemetry or usage limit of its own.

## Jev (required)

[Jev](https://typesafe.ai), by TypeSafe, is the model that chooses each action. It does not write free text:
it answers typed questions ("which of these actions?", "which of these elements?") with a choice and a
probability for every option. That keeps each decision small, fast and cheap, and it is why a step can be
checked against your safety rules before anything happens.

Two ways to reach it:

**Through the Vercel AI Gateway** (one key and one bill for several providers):

```bash
TYPESAFE_API_KEY=<your AI Gateway key>
TYPESAFE_URL=https://ai-gateway.vercel.sh/typesafe/v1/systemone
TYPESAFE_MODEL=typesafe-ai/jev
```

**TypeSafe directly:**

```bash
TYPESAFE_API_KEY=<your TypeSafe key>
# TYPESAFE_URL defaults to https://api.typesafe.ai/v1/systemone
# TYPESAFE_MODEL defaults to jev-latest
```

## The text helper (optional)

When a flow must type into a field, Jev first chooses which of the flow's named `values` fits. If none does,
EveryRole asks a text model to write something from the goal. Any OpenAI-compatible chat API works:

```bash
TEXT_MODEL_API_KEY=<key>
TEXT_MODEL_BASE_URL=https://ai-gateway.vercel.sh/v1     # or any OpenAI-compatible base URL
TEXT_MODEL=moonshotai/kimi-k3                           # a model id on that API
TEXT_MODEL_REASONING=none                               # optional: turn reasoning off
```

If your flows give a value for every field they fill, the text helper is never called and you can leave it
unset. If it is needed and missing, the flow is reported as "not checked" with
`TYPE_TEXT needs TEXT_MODEL_API_KEY`, never as an app failure.

## Where the settings come from

1. Environment variables (CI secrets and variables).
2. A `.env` file in the current folder (`--env-file` for another path). It never overrides a variable that is
   already set.

A model setting set to an empty string counts as unset, so an undefined CI variable does not hide your
`.env` value or the default.

## What it costs

Only flow steps call a model: roughly one Jev call per action, plus one per typed field to pick the value.
The access matrix, `validate` and `doctor` make no model calls, and each user type signs in once per run, not
once per flow. `results.json` records the decisions and input tokens of every flow (`decisions`,
`input_tokens`) and the run total (`summary.input_tokens`), so you can compute your own cost with your
provider's prices.

## What the models see

The goal, the page's URL, title and text, the list of controls, recent actions, and the names of the values.
Never secret values, never screenshots. Details: [Security model](security.md).
