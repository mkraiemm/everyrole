"""One unit of work per process; the result is printed as the last stdout line (JSON).

  login   sign in as a persona and save its session       -> FlowResult
  flow    run one flow (optionally with a saved session)  -> FlowResult
  access  check a persona's access rules (no model)       -> [AccessResult]

One process per unit gives each its own browser-harness daemon (BU_NAME), which Day 0 proved runs
correctly in parallel against one Chrome.
"""

import argparse
import json
import os
import sys
import time

from . import daemons, pace, processes
from .env import load_dotenv
from .session import load
from .site import resolve
from .spec import SpecError, load_config, load_flow_file, login_flow

RESULT_PREFIX = "EVERYROLE_RESULT "


def _emit(payload):
    print(RESULT_PREFIX + json.dumps(payload), flush=True)


def _parser():
    parser = argparse.ArgumentParser(prog="everyrole-worker")
    parser.add_argument("mode", choices=["login", "flow", "access"])
    parser.add_argument("--flow-file")
    parser.add_argument("--flow-name")
    parser.add_argument("--persona", default="")
    parser.add_argument("--path", action="append", default=[], help="access: only these pages (default: all)")
    parser.add_argument("--session", help="saved session to restore")
    parser.add_argument("--save-session", help="where a passing login saves its session")
    parser.add_argument("--base-url", required=True, help="address of the app's main part")
    parser.add_argument("--part", action="append", default=[], help="name=address of another part of the app")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--config")
    parser.add_argument("--output", default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--record-dir", help="login/flow: record frames here and make a replay video")
    return parser


def main(argv=None):
    try:
        return _main(argv)
    finally:
        stop_daemons()  # every mode, every exit path: no daemon outlives its worker


def stop_daemons():
    """Stops every browser-control daemon this worker started. The harness only stops a daemon it can verify, and
    removes its pid file either way: stop ours by that pid file first, then let it tidy up. A daemon too busy to
    answer when the next browser was opened was replaced, and that one pid file forgot it: then every process this
    worker started goes, recorded or not."""
    if os.environ.get(daemons.ENV):
        daemons.stop(os.environ[daemons.ENV])
    stop_own_daemon()
    processes.stop_children()


def _main(argv=None):
    args = _parser().parse_args(argv)
    load_dotenv(args.env_file)
    label = args.flow_name or f"{args.mode}@{args.persona}"
    try:
        config = load_config(args.config)
        site = resolve(config, base_url=args.base_url, parts=args.part)
        state = load(args.session) if args.session else None
        if args.mode == "access":
            from .access import AccessChecker, expected_rules

            rules = [r for r in expected_rules(config, args.persona) if not args.path or r[0] in args.path]
            results = AccessChecker(pace=pace.shared(config.run)).run(
                site=site, persona=args.persona, rules=rules, denied_when=config.denied_when,
                settle_s=config.access_settle_s, session_state=state,
            )
            _emit(_mark_signed_out([r.as_dict() for r in results], config, args.persona, site, state))
            return 0 if all(r.passed for r in results) else 1
        if args.mode == "login" and config.personas[args.persona].login_link:
            from .login_link import sign_in
            from .session import save

            result, signed = sign_in(config, args.persona, site=site, run_id=args.run_id)
            if signed is not None and args.save_session:
                save(signed, args.save_session)
            _emit(result)
            return 0 if result["passed"] else 1
        if args.mode == "login" and config.personas[args.persona].saved_signin:
            from .session import save
            from .signin import check

            result, saved = check(config, args.persona, site=site)
            if saved is not None and args.save_session:
                save(saved, args.save_session)
            _emit(result)
            return 0 if result["passed"] else 1
        if args.mode == "login":
            from . import kept_session

            kept = kept_session.reuse(config, args.persona, site=site)
            if kept is not None:  # still signed in by email from an earlier job: no new link to ask for
                from .session import save
                from .signin import _result

                if args.save_session:
                    save(kept[0], args.save_session)
                _emit({**_result(f"login@{args.persona}", args.persona, True, "passed", "", "", time.monotonic(),
                                 time.monotonic), "saved_signin": False, "kept_session": True,
                       "final_url": site.url(kept[1]) if site else kept[1]})
                return 0
            flow = login_flow(config, args.persona, site=site, run_id=args.run_id)
        else:
            flows = load_flow_file(args.flow_file, config=config, site=site, run_id=args.run_id)
            flow = next((f for f in flows if f.name == args.flow_name), None)
            if flow is None:
                raise SpecError(f"{args.flow_file}: no run named {args.flow_name!r}")
    except SpecError as exc:
        _emit({"name": label, "status": "error", "passed": False, "error": str(exc), "cause": "setup"})
        return 2

    from .runner import FlowRunner

    result = FlowRunner(pace=pace.shared(config.run)).run(flow, output_dir=args.output, session_state=state,
                                                          save_session_to=args.save_session,
                                                          record_dir=args.record_dir, site=site,
                                                          signed_out=_signed_out_check(config, flow, site, state))
    if flow.is_login and result.passed and result.signed_in_by == "email" and args.save_session:
        from . import kept_session

        kept_session.keep(config, flow.persona, args.save_session, site.page(result.final_url) if site else "/")
    _emit(result.as_dict())
    return 0 if result.passed else 1


def _signed_out_check(config, flow, site, state):
    """For a test that runs signed in: how to tell its session expired mid-run. None for a test that must be sent
    away (being refused is its point), one that changes its own password (suite.py signs in after it), a sign-in."""
    if state is None or not flow.persona or flow.is_login or flow.expect_denied or flow.changes_password:
        return None
    from .signin import SignedOut

    return SignedOut.of(config, flow.persona, site=site)


def _mark_signed_out(rows, config, persona, site, state):
    """Access rows of a signed-in user type: a page that should open but sent the browser to the sign-in page says
    the session expired mid-run (`signed_out`), not that the page is closed."""
    check = None
    if state is not None:
        from .signin import SignedOut

        check = SignedOut.of(config, persona, site=site)
    for row in rows:
        why = check.page(row.get("url")) if check and row["expected"] == "allow" and row["observed"] == "denied" else ""
        if why:
            row["signed_out"] = why
    return rows


def stop_own_daemon():
    """Each worker owns one browser-harness daemon (BU_NAME); stop it so none outlive the run."""
    name = os.environ.get("BU_NAME")
    if not name:
        return
    try:
        from browser_harness.admin import restart_daemon  # "restart" only stops; nothing is respawned

        restart_daemon(name)
    except Exception as exc:
        print(f"warning: could not stop browser-harness daemon {name}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
