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

from . import daemons, pace
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
        # every mode, every exit path: no daemon outlives its worker. The harness only stops a daemon it can
        # verify, and removes its pid file either way: stop ours by that pid file first, then let it tidy up.
        if os.environ.get(daemons.ENV):
            daemons.stop(os.environ[daemons.ENV])
        stop_own_daemon()


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
            _emit([r.as_dict() for r in results])
            return 0 if all(r.passed for r in results) else 1
        if args.mode == "login" and config.personas[args.persona].saved_signin:
            from .session import save
            from .signin import check

            result, saved = check(config, args.persona, site=site)
            if saved is not None and args.save_session:
                save(saved, args.save_session)
            _emit(result)
            return 0 if result["passed"] else 1
        if args.mode == "login":
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
                                                          record_dir=args.record_dir, site=site)
    _emit(result.as_dict())
    return 0 if result.passed else 1


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
