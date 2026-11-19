"""EveryRole CLI.

  everyrole doctor                              # is this machine ready? (Chrome, keys, provider, models)
  everyrole validate flows/                     # offline: schema + templates, no browser, no model calls
  everyrole run flows/ --base-url https://...   # run flows against a deploy; exit 1 if any flow fails
  everyrole signin --persona member             # try one kind of user's sign-in now, say where it landed
  everyrole ci --app my-app                     # in your CI: the app's tests, with logins only your CI has
"""

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from . import (
    account_book,
    account_cleanup,
    daemons,
    durations,
    flaky,
    limits,
    new_user_tests,
    pace,
    promotion,
    report,
    usage,
)
from .chrome import ChromeError, chrome_session
from .env import for_environment, load_dotenv
from .site import resolve
from .spec import (
    KEPT_OFF_REASON,
    SpecError,
    kept_off,
    load_config,
    load_flows,
    login_flow,
    without_logins,
    without_personas,
)
from .suite import SuiteRunner, site_checker


def _parser():
    parser = argparse.ArgumentParser(prog="everyrole", description="Does my app still work?")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate flow files without running them")
    validate.add_argument("flows", nargs="+", help="Flow files or directories")
    validate.add_argument("--base-url", help="Used to resolve {{BASE_URL}} (default: the config's environment, "
                                             "else https://example.invalid)")
    validate.add_argument("--env", dest="environment", default="production")
    validate.add_argument("--part", action="append", default=[], help="name=address of another part of the app")
    validate.add_argument("--env-file", default=".env")
    validate.add_argument("--config", help="everyrole.config.json (default: ./everyrole.config.json if present)")

    run = sub.add_parser("run", help="Run flows against a deployed app")
    run.add_argument("flows", nargs="+", help="Flow files or directories")
    run.add_argument("--base-url", help="Deploy URL of the app's main part ({{BASE_URL}}); default: its address in "
                                        "the config's `environments` for --env")
    run.add_argument("--env", dest="environment", default="production",
                     help="Environment: its addresses come from the config's `environments`; only flows whose "
                          "run_on has it run")
    run.add_argument("--part", action="append", default=[],
                     help="name=address of another part of the app (e.g. site=https://pr-12.example.com)")
    run.add_argument("--parallel", type=int, help="Tests at the same time (default: the config's run.parallel)")
    run.add_argument("--output", default="everyrole-report")
    run.add_argument("--cdp-url", default=os.environ.get("BU_CDP_URL"), help="Use this Chrome instead of launching")
    run.add_argument("--env-file", default=".env")
    run.add_argument("--config", help="everyrole.config.json (default: ./everyrole.config.json if present)")
    run.add_argument("--videos", action="store_true", help="Record a replay video of every flow (needs ffmpeg)")
    run.add_argument("--inject", help="Benchmarking: plant the bug described in this JSON file in every page")
    run.add_argument("--skip-access", action="store_true", help="Run only the given flows, not the access rules")
    run.add_argument("--rerun-of", default="", metavar="STAMP",
                     help="This runs a test of the run runs/STAMP again: the report says so, and that run's page "
                          "shows the new result beside the one it recorded")
    run.add_argument("--accounts-file",
                     help="The accounts this run (or its change) created on the app, one per role (account_book.py); "
                          "default: accounts.json in --output")
    run.add_argument("--keep-accounts", action="store_true",
                     help="Don't delete them at the end: a change's walks share them; it deletes them when it ends")
    run.add_argument("--new-version", action="store_true",
                     help="A deploy started this run: earlier runs tested another version (never flaky across it)")
    run.add_argument("--skip-long", action="store_true",
                     help="A deploy's check: leave out the tests measured long (durations.py); the daily run has them")
    run.add_argument("--skip-sign-up", action="store_true",
                     help="Leave out the tests that sign up a new user (their environment's schedule says not this "
                          "run: new_user_tests.py)")
    run.add_argument("--skip-delete", action="store_true",
                     help="Leave out the tests that delete (their environment's schedule says not this run)")
    run.add_argument("--no-logins", action="store_true",
                     help="Leave out every kind of user that signs in (an app whose tests run in its own CI keeps no "
                          "test login on this machine): only visitors' tests and page checks run")
    run.add_argument("--no-flaky", action="store_true",
                     help="Never set a test aside as flaky: each run judges one version of the app (a change's runs)")

    signin = sub.add_parser("signin", help="Try one kind of user's sign-in now and say where it landed")
    signin.add_argument("--persona", required=True)
    signin.add_argument("--config")
    signin.add_argument("--base-url")
    signin.add_argument("--env", dest="environment", default="production")
    signin.add_argument("--part", action="append", default=[])
    signin.add_argument("--env-file", default=".env")
    signin.add_argument("--output", help="Also write the result as JSON here")
    signin.add_argument("--cdp-url", default=os.environ.get("BU_CDP_URL"))

    doctor = sub.add_parser("doctor", help="Check that this machine is ready to test (a tiny call to each model)")
    doctor.add_argument("--env-file", default=".env")

    from .ci import add_parser as add_ci

    add_ci(sub)
    comment = sub.add_parser("comment", help="Post/update the report as one PR comment (GitHub Actions)")
    comment.add_argument("--report", default="everyrole-report/summary.md")
    return parser


def cmd_doctor(args):
    from jev_ultrafast.model import typesafe_url

    from . import doctor

    load_dotenv(args.env_file)
    from .model_check import probe

    text, code = doctor.report(doctor.run(provider_url=typesafe_url(), probe=probe))
    print(text)
    return code


def cmd_comment(args):
    from .github import GitHubError, post_report

    try:
        print(post_report(args.report))
    except GitHubError as exc:
        print(f"warning: {exc}", file=sys.stderr)  # a failed comment must not hide the test result
    return 0


def cmd_signin(args):
    """One sign-in, exactly as a run does it (typed password, or a saved sign-in checked), then nothing else."""
    import json
    import tempfile
    import time

    load_dotenv(args.env_file)
    for_environment(args.environment)  # this environment's own secrets (staging's test code...)
    config = load_config(args.config)
    site = resolve(config, base_url=args.base_url, environment=args.environment, parts=args.part)
    if args.persona not in config.personas or not config.personas[args.persona].login:
        print(f"error: {args.persona!r} is not a kind of user that signs in", file=sys.stderr)
        return 2
    limits.contain(config.run)  # before the browser starts
    pace.shared(config.run)  # the sign-in's page waits for the network like any other
    with tempfile.TemporaryDirectory(prefix="everyrole-signin-") as tmp, chrome_session(args.cdp_url) as chrome:
        runner = SuiteRunner(parallel=1, cdp_url=chrome.url, chrome=chrome, site=site, output_dir=Path(tmp),
                             env_file=Path(args.env_file).resolve(),
                             config_path=Path(config.source).resolve() if config.source else None)
        timeout_s = config.login.get("timeout_s", 120) if config.login else 60
        from .kept_session import FRESH_ENV

        # a real sign-in (accepting an invitation, "Check sign-in"): never a session kept from an earlier job
        result = runner._login(args.persona, Path(tmp) / "session.json", timeout_s, env_extra={FRESH_ENV: "1"})
    page = site.page(result.get("final_url") or "")  # where it landed, as people read it
    from .signin import why_not_signed_in
    from .verdict import APP, cause_of

    # its cause says who reads its error: one written for people (a sign-in problem) or ours (a machine problem)
    cause = "" if result.get("passed") else (result.get("cause") or cause_of(result))
    error = result.get("error") or ""
    if cause == APP:  # the sign-in itself didn't go through: the engine's words never reach people, a reason does
        error = why_not_signed_in(result, site.label(page) if page else "")
    outcome = {"persona": args.persona, "passed": bool(result.get("passed")),
               "cause": cause, "error": error, "at": time.time(),
               "signed_in_by": result.get("signed_in_by") or "",
               "landed": site.label(page) if page else result.get("final_url") or ""}
    print(("signed in, landed on " + outcome["landed"]) if outcome["passed"] else
          f"sign-in failed: {outcome['error'] or result.get('status')}")
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    return 0 if outcome["passed"] else 1



def cmd_validate(args):
    load_dotenv(args.env_file)
    for_environment(args.environment)  # this environment's own secrets (staging's test code...)
    config = load_config(args.config)
    base_url = args.base_url or (None if config.environments else "https://example.invalid")  # offline check
    site = resolve(config, base_url=base_url, environment=args.environment, parts=args.part)
    for persona in (p for p, spec in config.personas.items() if spec.login and not spec.signs_in_alone):
        login_flow(config, persona, site=site, run_id="validate")
        print(f"ok  login@{persona}  ({config.source})")
    for flow in load_flows(args.flows, config=config, site=site, run_id="validate"):
        print(f"ok  {flow.name}  ({flow.source})")
    for path, rule in config.access.items():
        print(f"ok  access {path}  allow={rule.get('allow', [])} deny={rule.get('deny', [])}")
    return 0


def _clean_accounts(book, config, runner_args, chrome, folder):
    """Delete the accounts this run created, the app's own way (account_cleanup.py), and say how it went."""
    if not book.to_clean():
        return
    from .suite import SuiteRunner

    def run(journeys):
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for journey in journeys:
            path = folder / f"{journey['name']}.json"
            path.write_text(json.dumps(journey, indent=2), encoding="utf-8", newline="\n")
            paths.append(str(path))
        flows = load_flows(paths, config=config, site=runner_args["site"], run_id=uuid.uuid4().hex[:8])
        quiet = {**runner_args, "output_dir": folder, "videos": False}
        runner = SuiteRunner(cdp_url=chrome.url, chrome=chrome, **quiet)
        suite = runner.run(flows, replace(config, access={}))
        return {r["name"]: r for r in suite["results"]}

    for email, state in account_cleanup.clean(book, config, run):
        print(f"· test account {email}: {account_book.NOTES[state]}", flush=True)


def cmd_run(args):
    load_dotenv(args.env_file)
    for_environment(args.environment)  # this environment's own secrets (staging's test code...)
    if args.inject:
        from .inject import ENV

        os.environ[ENV] = str(Path(args.inject).resolve())  # inherited by every worker
    missing = [k for k in ("TYPESAFE_API_KEY",) if not os.environ.get(k)]
    if missing:
        print(f"error: missing env {', '.join(missing)}", file=sys.stderr)
        return 2
    config = load_config(args.config)
    site = resolve(config, base_url=args.base_url, environment=args.environment, parts=args.part)
    if args.skip_access:  # e.g. running one test by hand: the page-access checks are not part of it
        config = replace(config, access={})
    skipped = frozenset()
    if getattr(args, "no_logins", False):  # the app's logins live only in its own CI (everyrole ci)
        config, skipped = without_logins(config)
        if skipped:
            print(f"· Tests that sign in as {', '.join(sorted(skipped))} run in your own CI, where their logins are: "
                  "only visitors' tests run here", flush=True)
    output = Path(args.output)
    # one account per role for the whole run, or the whole change (its walks share the file)
    book_path = Path(args.accounts_file) if getattr(args, "accounts_file", None) else output / account_book.FILE
    os.environ[account_book.ENV] = str(book_path.resolve())  # inherited by every worker
    runner_args = dict(
        parallel=args.parallel or config.run["parallel"], site=site, output_dir=output,
        env_file=Path(args.env_file).resolve(),
        config_path=Path(config.source).resolve() if config.source else None, videos=args.videos,
    )
    run_id = uuid.uuid4().hex[:8]  # same id in flow templates ({{RUN_ID}}) and in every worker
    loaded = load_flows(args.flows, config=config, site=site, run_id=run_id, skip_personas=skipped)
    trials = [f for f in loaded if promotion.on_trial(f, args.environment)]  # found on staging: tried here once
    flows = [f for f in loaded if f.runs_in(args.environment)] + trials
    if getattr(args, "skip_long", False):  # a deploy's check never waits for the app's slow work
        flows, long = durations.without_long(flows, flaky.past_runs(output))
        for name, ms in long.items():
            print(f"· {name}: long ({durations.spoken(ms)}), not in a deploy's check: it runs with the daily run "
                  "and when run by hand", flush=True)
    for _setting, label, of_kind, option in new_user_tests.KINDS.values():
        if getattr(args, option[2:].replace("-", "_"), False):  # each one makes or destroys something real
            for name in [f.name for f in flows if of_kind(f)]:
                print(f"· {name}: {label.lower()} are off for this run (Settings → Sign-up and payments says when "
                      "they run)", flush=True)
            flows = [f for f in flows if not of_kind(f)]
    off = kept_off(config, args.environment)  # an admin on production, until the team allows it there
    if off:
        for name in sorted(off):
            print(f"· {name}: {KEPT_OFF_REASON}", flush=True)
        for name in [f.name for f in flows if f.persona in off]:
            print(f"· {name}: not run on production: {KEPT_OFF_REASON.split(', ', 1)[0]}", flush=True)
        flows = [f for f in flows if f.persona not in off]
        config = without_personas(config, off)
    if not flows and not config.access:
        print(f"No flows run in environment {args.environment!r}.")
        return 0
    job_limits = limits.contain(config.run)  # before the browser starts: everything after is inside the caps
    print(f"· {job_limits.describe()}", flush=True)
    print(f"· {usage.describe_models()}", flush=True)
    pace.watch(pace.shared(config.run), lambda line: print(line, flush=True))  # one network pace for the run
    with chrome_session(args.cdp_url) as chrome:
        suite = SuiteRunner(cdp_url=chrome.url, chrome=chrome, run_id=run_id, site_check=site_checker(),
                            **runner_args).run(flows, config)
        if not getattr(args, "keep_accounts", False):
            _clean_accounts(account_book.AccountBook(book_path), config, runner_args, chrome, output / "cleanup")
    print(f"  {job_limits.report()}", flush=True)
    tried = {f.name for f in trials}
    trial_results = [r for r in suite["results"] if r.get("name") in tried]
    suite["results"] = [r for r in suite["results"] if r.get("name") not in tried]  # never a failure or alert
    settled = promotion.settle(trials, trial_results)
    for name in settled["promoted"]:
        print(f"· {name}: found on {next(f.found_on for f in trials if f.name == name)}, works on production: "
              "promoted, it runs everywhere now", flush=True)
    if settled["not_live"]:
        print(f"· not live on production yet (quiet): {', '.join(settled['not_live'])}", flush=True)
    here = {"environment": args.environment, "base_url": site.main_url,
            "new_version": bool(getattr(args, "new_version", False))}
    if not getattr(args, "no_flaky", False):  # a change's runs are one version of the app each: never flaky
        flaky.mark(suite["results"], here, flaky.past_runs(output), runs=config.run["flaky_runs"],
                   clears_after=config.run["flaky_clears_after"])  # set aside: never a failure, never an alert
    summary = report.summarize(suite, base_url=site.main_url, environment=args.environment, parts=dict(site.parts))
    summary["trials"] = settled
    summary["new_version"] = here["new_version"]  # a deploy started it (flaky.py counts flips within one version)
    if getattr(args, "rerun_of", ""):
        summary["rerun_of"] = args.rerun_of  # the run whose test this runs again (views.attach_reruns)
    report.write(output, summary, suite["results"], suite["access"])
    record = usage.write(output, kind="run", groups=suite["usage"], started=time.time() - suite["duration_ms"] / 1000,
                         detail=f"{summary['total']} tests, {summary.get('access_total', 0)} page checks",
                         counts={"tests": summary["total"], "page_checks": summary.get("access_total", 0)})
    print(f"  used: {sum(g.get('calls', 0) for g in record['groups'].values())} model calls, "
          f"${record['cost_usd']:.4f}" + ("" if record["cost_known"] else
                                          f" (no price reported for {', '.join(record['unpriced'])})"), flush=True)
    print(report.markdown(summary, suite["results"], suite["access"]))
    print(f"Report: {output.resolve()}")
    return exit_code(summary)


def exit_code(summary):
    """1 = the app has a problem. A machine problem never fails the build as an app failure: it only
    exits 2 ("EveryRole could not test") when nothing at all could be checked."""
    if summary["failed"]:
        return 1
    checks = summary["total"] + summary.get("access_total", 0)
    answered = checks - summary.get("not_checked", 0) - summary.get("not_run", 0)
    if summary.get("not_checked") and answered <= 0:
        print("EveryRole could not check anything (machine problem, not your app).", file=sys.stderr)
        return 2
    if summary.get("not_checked"):
        print(f"warning: {summary['not_checked']} checks could not be run (machine problem, not your app)",
              file=sys.stderr)
    return 0


# commands whose workers start browser-control daemons
BROWSER_COMMANDS = {"run", "signin"}


def cmd_ci(args):
    from .ci import cmd_ci as run_ci

    return run_ci(args)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    for stream in (sys.stdout, sys.stderr):  # a Windows console can't print every sign of a report: replace them
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(errors="replace")
    args = _parser().parse_args(argv)
    if args.command in BROWSER_COMMANDS:
        daemons.sweep()  # what earlier runs killed from outside left running
        daemons.claim()  # this job's daemon folder: every worker's daemons are found and stopped at the end
    try:
        commands = {"validate": cmd_validate, "run": cmd_run, "comment": cmd_comment, "doctor": cmd_doctor,
                    "signin": cmd_signin, "ci": cmd_ci}
        return commands[args.command](args)
    except (SpecError, ChromeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.command in BROWSER_COMMANDS:
            daemons.release()  # none outlives the job


if __name__ == "__main__":
    sys.exit(main())
