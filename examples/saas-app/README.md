# Example: a made-up SaaS app with four user types

Everything here is fictional (the app, the pages, the accounts). It shows how a real project is laid out;
it cannot run as is, because there is no app behind it. Point `--base-url` at your own app and adapt the
paths and labels.

```
everyrole.config.json   how to sign in, the user types, and who may open which page
flows/               one plain-English check per file
```

- **User types (`personas`)**: `admin`, `pro` and `free` sign in with accounts from your `.env`
  (`ADMIN_EMAIL`, `ADMIN_PASSWORD`, ...); `guest` is a signed-out visitor.
- **Access rules (`access`)**: checked with no AI at all. A page counts as refused when the app sends the
  user somewhere else, or when a `denied_when` check matches (here: an in-place "no access" message).
- **`reports-by-plan.json`** runs twice, once per user type, each with its own checks. The free run uses
  `expect_denied`: the check passes when the app refuses and shows the upgrade prompt.
- **`invite-teammate-form.json`** fills and submits the invite form with saving blocked, and passes only if
  the page really tried to send the invitation. It runs on preview deploys only (`run_on`).

Check the files without a browser or AI key:

```bash
everyrole validate examples/saas-app/flows --config examples/saas-app/everyrole.config.json --base-url https://app.example.com
```

`validate` needs every `{{secrets.NAME}}` to exist: put dummy values in your `.env` to try it.
