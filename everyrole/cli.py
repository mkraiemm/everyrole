"""EveryRole CLI.

  everyrole doctor                              # is this machine ready? (Chrome, keys, provider)
  everyrole validate flows/                     # offline: schema + templates, no browser, no model calls
  everyrole run flows/ --base-url https://...   # run flows against a deploy; exit 1 if any flow fails
  everyrole signin --persona member             # try one kind of user's sign-in now, say where it landed
  everyrole comment                             # post the report as one pull-request comment (GitHub Actions)
"""

import argparse
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from . import daemons, limits, pace, report, usage
from .chrome import ChromeError, chrome_session
from .env import load_dotenv
from .site import resolve
from .spec import SpecError, load_config, load_flows, login_flow
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
    run.add_argument("--inject", help="Plant the bug described in this JSON file in every page, to check that "
                                       "your flows catch it")
    run.add_argument("--skip-access", action="store_true", help="Run only the given flows, not the access rules")

    signin = sub.add_parser("signin", help="Try one kind of user's sign-in now and say where it landed")
    signin.add_argument("--persona", required=True)
    signin.add_argument("--config")
    signin.add_argument("--base-url")
    signin.add_argument("--env", dest="environment", default="production")
    signin.add_argument("--part", action="append", default=[])
    signin.add_argument("--env-file", default=".env")
    signin.add_argument("--output", help="Also write the result as JSON here")
    signin.add_argument("--cdp-url", default=os.environ.get("BU_CDP_URL"))

    doctor = sub.add_parser("doctor", help="Check that this machine is ready to test (no model calls)")
    doctor.add_argument("--env-file", default=".env")

    comment = sub.add_parser("comment", help="Post/update the report as one PR comment (GitHub Actions)")
    comment.add_argument("--report", default="everyrole-report/summary.md")
    return parser


def cmd_doctor(args):
    from jev_ultrafast.model import typesafe_url

    from . import doctor

    load_dotenv(args.env_file)
    text, code = doctor.report(doctor.run(provider_url=typesafe_url()))
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
        result = runner._login(args.persona, Path(tmp) / "session.json", timeout_s)
    page = site.page(result.get("final_url") or "")  # where it landed, as people read it
    outcome = {"persona": args.persona, "passed": bool(result.get("passed")), "cause": result.get("cause", ""),
               "error": result.get("error") or "", "at": time.time(),
               "landed": site.label(page) if page else result.get("final_url") or ""}
    print(("signed in, landed on " + outcome["landed"]) if outcome["passed"] else
          f"sign-in failed: {outcome['error'] or result.get('status')}")
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    return 0 if outcome["passed"] else 1


def cmd_validate(args):
    load_dotenv(args.env_file)
    config = load_config(args.config)
    base_url = args.base_url or (None if config.environments else "https://example.invalid")  # offline check
    site = resolve(config, base_url=base_url, environment=args.environment, parts=args.part)
    for persona in (p for p, spec in config.personas.items() if spec.login and not spec.saved_signin):
        login_flow(config, persona, site=site, run_id="validate")
        print(f"ok  login@{persona}  ({config.source})")
    for flow in load_flows(args.flows, config=config, site=site, run_id="validate"):
        print(f"ok  {flow.name}  ({flow.source})")
    for path, rule in config.access.items():
        print(f"ok  access {path}  allow={rule.get('allow', [])} deny={rule.get('deny', [])}")
    return 0


def cmd_run(args):
    load_dotenv(args.env_file)
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
    output = Path(args.output)
    runner_args = dict(
        parallel=args.parallel or config.run["parallel"], site=site, output_dir=output,
        env_file=Path(args.env_file).resolve(),
        config_path=Path(config.source).resolve() if config.source else None, videos=args.videos,
    )
    run_id = uuid.uuid4().hex[:8]  # same id in flow templates ({{RUN_ID}}) and in every worker
    flows = [f for f in load_flows(args.flows, config=config, site=site, run_id=run_id)
             if f.runs_in(args.environment)]
    if not flows and not config.access:
        print(f"No flows run in environment {args.environment!r}.")
        return 0
    job_limits = limits.contain(config.run)  # before the browser starts: everything after is inside the caps
    print(f"· {job_limits.describe()}", flush=True)
    pace.watch(pace.shared(config.run), lambda line: print(line, flush=True))  # one network pace for the run
    with chrome_session(args.cdp_url) as chrome:
        suite = SuiteRunner(cdp_url=chrome.url, chrome=chrome, run_id=run_id, site_check=site_checker(),
                            **runner_args).run(flows, config)
    print(f"  {job_limits.report()}", flush=True)
    summary = report.summarize(suite, base_url=site.main_url, environment=args.environment, parts=dict(site.parts))
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


BROWSER_COMMANDS = {"run", "signin"}  # commands whose workers start browser-control daemons


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command in BROWSER_COMMANDS:
        daemons.claim()  # one folder where every worker's browser-control daemon registers
    try:
        commands = {"validate": cmd_validate, "run": cmd_run, "comment": cmd_comment, "doctor": cmd_doctor,
                    "signin": cmd_signin}
        return commands[args.command](args)
    except (SpecError, ChromeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.command in BROWSER_COMMANDS:
            daemons.release()  # none outlives the job


if __name__ == "__main__":
    sys.exit(main())
