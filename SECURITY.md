# Security policy

EveryRole handles test-account credentials and drives a browser against your app, so we take reports seriously.

## Reporting a vulnerability

Please do **not** open a public issue. Report it privately through GitHub:
**Security → Report a vulnerability** on https://github.com/mkraiemm/everyrole (private vulnerability reporting).

Include what you found, how to reproduce it, and the impact you see. We will acknowledge the report within
3 working days, keep you updated, and credit you in the release notes unless you prefer otherwise.

Especially in scope:

- a secret value reaching a model request, a report, a log, a PR comment or an artifact;
- a way around the write-guard, `forbid` or `allowed_hosts` from page content;
- session files leaking outside their private temporary folder;
- command or script injection through flow files, config files or GitHub Action inputs.

Known limits of the write-guard (service workers, WebSockets, writes made by GET requests) are documented in
[docs/security.md](docs/security.md) and are not vulnerabilities by themselves.

## Supported versions

Security fixes are released for the latest 0.x version.
