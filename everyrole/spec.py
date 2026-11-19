"""Flow and config files: schema validation, schema defaults, persona expansion, template interpolation.

Templates: `{{BASE_URL}}`, `{{BASE_HOST}}` (the main address being tested), `{{URL.<part>}}` (the address of
another part of the app, see site.py), `{{RUN_ID}}` (unique per suite run) and `{{secrets.NAME}}` (env var NAME).
Every secret a flow uses is tracked so it can be redacted everywhere.
"""

import json
import os
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from . import account_book, mailbox, password_change
from . import importance as importance_levels
from .errors import SpecError
from .site import Site
from .site import split as split_page

TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
CONFIG_FILE = "everyrole.config.json"


@dataclass(frozen=True)
class Flow:
    name: str
    start: str
    goal: str
    expect: tuple
    values: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)  # env var name -> literal, for every secret the flow uses
    forbid: tuple = ()
    allowed_hosts: tuple = ()
    max_steps: int = 0
    timeout_s: int = 0
    settle_s: int = 0
    submit_settle_s: int = 0
    scroll_retries: int = 0
    run_on: tuple = ()
    found_on: str = ""  # the environment exploring found it on, when not production (promotion.py)
    measured_ms: int = 0  # its time in the change's run it was kept from (durations.py), until it runs as a test
    source: str = ""
    persona: str = ""  # "" = no persona (fresh signed-out browser)
    expect_denied: bool = False
    block_writes: bool = False
    ignore_saves: tuple = ()
    is_login: bool = False
    acts: bool = False  # it types its own values (a form): it must do its task before its checks count
    changes_password: str = ""  # the user type whose own password it changes (password_change.py)
    not_run: str = ""  # why it can't run here (e.g. no new password prepared): reported, never an error
    deletes: bool = False  # it presses Delete or Remove account: runs only when scheduled (new_user_tests.py)
    importance: str = importance_levels.DEFAULT  # critical | important | minor (importance.py)
    # a journey: ({"goal", "expect", "reload", "sign_in_as", "sign_up", "open_email", "email_wait_s"}, ...) done in
    # order in one browser, each step judged by its own checks; `goal` is then the first step's and `expect` the last
    # step's (see runner.FlowRunner)
    steps: tuple = ()
    inbox: str = ""  # a journey that signs up: the folder its new account's emails land in (mailbox.new_account)
    waiting: bool = False  # a change's journey not yet proven on production by a release (releases.py)

    def runs_in(self, environment):
        if self.waiting and environment == PRODUCTION:  # its change isn't known to be live there yet
            return False
        return not self.run_on or environment in self.run_on

    @property
    def budget_s(self):
        """Its whole run's time: the test's time for each step, plus every wait a step is allowed (slow work, an
        email), so a journey that waits for a 50-minute film is never cut off as hung (suite.py)."""
        waits = sum(step.get("wait_s", 0) + (step.get("email_wait_s", 0) if step.get("open_email") else 0)
                    for step in self.steps)
        return self.timeout_s * max(1, len(self.steps)) + waits


@dataclass(frozen=True)
class Persona:
    name: str
    values: dict  # templates, interpolated per flow so secrets are tracked per flow
    login: bool = True
    start_paths: tuple = ()
    saved_signin: dict = None  # {"file", "check"}: a sign-in a person did by hand, reused instead of typing
    signin: dict = None  # {"start", "expect"}: its own sign-in page, when not the app's (login.start)
    signin_by: str = ""  # "email": it signs in by the link the app emails (an invited account made that way)
    login_link: dict = None  # {"url", "check"}: the app's own endpoint hands a one-time sign-in link (login_link.py)
    production: bool = True  # False: never explored or tested on production (an admin, until allowed there)

    @property
    def signs_in_alone(self):
        """Signed in without typing anything: a sign-in a person did by hand, or the app's login link."""
        return bool(self.saved_signin or self.login_link)


@dataclass(frozen=True)
class Config:
    login: dict = field(default_factory=dict)
    personas: dict = field(default_factory=dict)  # name -> Persona
    access: dict = field(default_factory=dict)  # path -> {"allow": [...], "deny": [...]}
    denied_when: tuple = ()
    access_settle_s: int = 0
    discover: dict = field(default_factory=dict)
    environments: dict = field(default_factory=dict)  # name -> {part: address}; see site.py
    run: dict = field(default_factory=lambda: _defaults({}, schema("config.schema.json")["properties"]["run"]
                                                        ["properties"]))  # schema defaults filled in
    source: str = ""

    @property
    def parts(self):
        """The app's part names, main part first ([] for a one-address app given on the command line)."""
        return list(next(iter(self.environments.values()))) if self.environments else []

    def persona(self, name, source):
        if name not in self.personas:
            known = ", ".join(sorted(self.personas)) or "none defined"
            raise SpecError(f"{source}: unknown persona {name!r} (personas in {CONFIG_FILE}: {known})")
        return self.personas[name]


# ---------------------------------------------------------------- schemas


@lru_cache(maxsize=None)
def schema(name="flow.schema.json"):
    return json.loads(resources.files("everyrole.schema").joinpath(name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _registry():
    names = ("flow.schema.json", "config.schema.json")
    return Registry().with_resources([(schema(n)["$id"], Resource.from_contents(schema(n))) for n in names])


def _validate(raw, schema_name, source):
    validator = Draft202012Validator(
        schema(schema_name), registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if errors:
        details = "; ".join(f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors)
        raise SpecError(f"{source}: {details}")


def _defaults(raw, properties):
    merged = dict(raw)
    for key, definition in properties.items():
        if key not in merged and "default" in definition:
            merged[key] = json.loads(json.dumps(definition["default"]))
    return merged


# ---------------------------------------------------------------- templates


def site_of(base_url=None, site=None):
    """The site a flow runs against: `site` when given, else a one-part site at `base_url` (or none)."""
    if site is not None:
        return site
    return Site.single(base_url) if base_url else None


class _Interpolator:
    def __init__(self, site, env, source, run_id=""):
        self.site = site
        self.context = {
            "BASE_URL": site.main_url if site else "",
            "BASE_HOST": urlparse(site.main_url).hostname if site else "",
            "RUN_ID": run_id,
        }
        for name, url in (site.parts if site else ()):
            self.context[f"URL.{name}"] = url
        self.env = env
        self.source = source
        self.used_secrets = set()

    def __call__(self, text, where):
        def replace(match):
            key = match.group(1)
            if key.startswith("secrets."):
                name = key.split(".", 1)[1]
                if name not in self.env or self.env[name] == "":
                    raise SpecError(f"{self.source}: {where} needs env var {name} (from {{{{secrets.{name}}}}})")
                self.used_secrets.add(name)
                return self.env[name]
            if key in self.context:
                if not self.context[key]:
                    hint = "no --base-url was given" if key.startswith("BASE") else "no run id is available"
                    raise SpecError(f"{self.source}: {where} uses {{{{{key}}}}} but {hint}")
                return self.context[key]
            if key.startswith("URL."):
                known = ", ".join(self.site.names) if self.site else "none: no address was given"
                raise SpecError(f"{self.source}: {where} uses {{{{{key}}}}} but the app has no part "
                                f"{key[4:]!r} (parts: {known})")
            raise SpecError(f"{self.source}: {where} has unknown template {{{{{key}}}}}")

        return TEMPLATE.sub(replace, text)


# ---------------------------------------------------------------- config


def load_config(path=None):
    """Load everyrole.config.json. A missing default file means: no personas, no access rules."""
    if path is None:
        path = Path(CONFIG_FILE)
        if not path.is_file() and Path("sudoqa.config.json").is_file():
            raise SpecError("found sudoqa.config.json: the tool is now EveryRole and reads everyrole.config.json. "
                            "Rename the file (its content stays the same).")
        if not path.is_file():
            props = schema("config.schema.json")["properties"]
            return Config(discover=_defaults({}, props["discover"]["properties"]),
                          run=_defaults({}, props["run"]["properties"]))
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"{path}: config file not found") from None
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None
    _validate(raw, "config.schema.json", str(path))
    props = schema("config.schema.json")["properties"]
    data = _defaults(raw, props)
    persona_props = props["personas"]["additionalProperties"]["properties"]
    personas = {}
    for name, definition in data["personas"].items():
        merged = _defaults(definition, persona_props)
        personas[name] = Persona(name=name, values=merged["values"], login=merged["login"],
                                 start_paths=tuple(merged["start_paths"]), saved_signin=merged.get("saved_signin"),
                                 signin=merged.get("signin"), signin_by=merged.get("signin_by", ""),
                                 login_link=merged.get("login_link"), production=merged.get("production", True))
    login = _defaults(data["login"], props["login"]["properties"]) if "login" in data else {}
    if any(p.login and not p.signs_in_alone for p in personas.values()) and not login:
        raise SpecError(f"{path}: personas that sign in need a `login` section")
    environments = data.get("environments", {})
    part_names = list(next(iter(environments.values()))) if environments else []
    for name, addresses in environments.items():
        if list(addresses) != part_names:
            raise SpecError(f"{path}: environment {name!r} must list the same parts, in the same order, as the "
                            f"first one ({', '.join(part_names)})")
    for route, rule in data["access"].items():
        part = split_page(route)[0]
        if part and part not in part_names[1:]:
            raise SpecError(f"{path}: access[{route!r}] is on part {part!r}, which is not one of the app's other "
                            f"parts ({', '.join(part_names[1:]) or 'none'})")
        for persona in [*rule.get("allow", []), *rule.get("deny", [])]:
            if persona not in personas:
                raise SpecError(f"{path}: access[{route!r}] uses unknown persona {persona!r}")
    return Config(
        login=login,
        personas=personas,
        access=data["access"],
        denied_when=tuple(data.get("denied_when", ())),
        access_settle_s=data["access_settle_s"],
        discover=_defaults(data["discover"], props["discover"]["properties"]),
        environments=environments,
        run=_defaults(data["run"], props["run"]["properties"]),
        source=str(path),
    )


# ---------------------------------------------------------------- flows


def _variants(data):
    """(persona, expect, expect_denied) for each run this flow expands to."""
    who = data.get("as")
    if who is None:
        return [("", data.get("expect"), data["expect_denied"])]
    if isinstance(who, str):
        return [(who, data.get("expect"), data["expect_denied"])]
    return [
        (name, entry.get("expect", data.get("expect")), entry.get("expect_denied", data["expect_denied"]))
        for name, entry in who.items()
    ]


def _blocks_writes(data, expect):
    """Saves are blocked when the test says so, and always for a test that only checks a save was attempted
    ({"tried_to_save": ...}): without the block that save really happens (a "suspend" check would suspend someone).
    A test meant to change data says so with `run_on`; the password-change test changes the password for real."""
    if data.get("block_writes"):
        return True
    if data.get("run_on") or data.get("changes_password"):
        return False
    return any(isinstance(check, dict) and "tried_to_save" in check for check in expect or ())


def _build(data, *, name, persona, expect, expect_denied, values_templates, fill, source, is_login=False,
           acts=False):
    """`source` is the file path (used to re-load the flow); `fill.source` is the label used in errors."""
    values = {k: fill(v, f"values.{k}") for k, v in values_templates.items()}
    start = fill(data["start"], "start")
    if urlparse(start).scheme not in {"http", "https"}:
        raise SpecError(f"{fill.source}: start must be an http(s) URL, got {start!r}")
    checks = _filled_checks(expect, fill, "expect")
    # by default a flow may go anywhere in the app (every part), never outside it
    default_hosts = (*(fill.site.hosts if fill.site else ()), urlparse(start).hostname)
    allowed = tuple(fill(h, "allowed_hosts") for h in data.get("allowed_hosts", ())) or tuple(
        dict.fromkeys(default_hosts))
    steps = tuple({"goal": fill(step["goal"], f"steps.{n}.goal") if step.get("goal") else "",
                   "hint": _hint(fill(step["goal"], f"steps.{n}.goal"), step) if step.get("goal") else "",
                   "expect": _filled_checks(step["expect"], fill, f"steps.{n}.expect"),
                   "reload": bool(step.get("reload")), "sign_in_as": step.get("sign_in_as", ""),
                   "sign_up": bool(step.get("sign_up")), "open_email": bool(step.get("open_email")),
                   "email_if_needed": bool(step.get("email_if_needed")),
                   "checked_by_next": bool(step.get("checked_by_next")),
                   # the step only reads the page the step before it led to (a way in): its checks must tell that
                   # page apart from the one the link was on (runner._discriminating)
                   "reached_by_previous": bool(step.get("reached_by_previous")),
                   "click": dict(step["click"]) if step.get("click") else None,
                   "submit": dict(step["submit"]) if step.get("submit") else None,
                   "unsure": bool(step.get("unsure")),
                   "email_wait_s": int(step.get("email_wait_s") or 180),
                   "wait_s": int(step.get("wait_s") or 0)}
                  for n, step in enumerate(data.get("steps") or ()))
    goal = fill(data["goal"], "goal") if data.get("goal") else (
        steps[0]["goal"] + steps[0]["hint"] if steps[0]["goal"] else _step_label(steps[0]))
    forbid = tuple(fill(p, "forbid") for p in data.get("forbid", ()))
    return Flow(
        name=name,
        start=start,
        goal=goal,
        expect=checks,
        values=values,
        secrets={n: fill.env[n] for n in sorted(fill.used_secrets)},
        forbid=forbid,
        allowed_hosts=allowed,
        max_steps=data["max_steps"],
        timeout_s=data["timeout_s"],
        settle_s=data["settle_s"],
        submit_settle_s=data.get("submit_settle_s", 0),
        scroll_retries=data.get("scroll_retries", 0),
        run_on=tuple(data.get("run_on", ())),
        found_on=data.get("found_on", ""),
        measured_ms=int(data.get("measured_ms") or 0),
        source=source,
        persona=persona,
        expect_denied=expect_denied,
        block_writes=_blocks_writes(data, expect),
        ignore_saves=tuple(data.get("ignore_saves", ())),
        is_login=is_login,
        acts=acts,
        importance=importance_levels.level(data.get("importance")),
        deletes=bool(data.get("deletes")),
        steps=steps,
        waiting=waiting_for_production(data),
    )


# an app's /login?mode=signup can open on its Sign in tab: the new account was typed there ("Invalid login
# credentials")
SIGN_UP_HINT = (" (create a brand-new account with new_account_email, new_account_password and new_account_name, in"
                " the form the app showed you, with its email and password fields: never a \"Continue with\" button"
                " of another company's account. A page that offers both signing in and creating an account (two tabs,"
                " a switch) may open on signing in: switch to creating an account before typing, and never press a"
                " Sign in button here; if the app says this email already has an account, log in with it)")
# this run's account was created already (account_book.py): one account per role, every later journey logs in
EXISTING_ACCOUNT_HINT = (" (the account already exists: log in with new_account_email and new_account_password, from"
                         " the page that asks, and never create another one; only if the app says no account has this"
                         " email, create it with new_account_email, new_account_password and new_account_name)")
# at a wall, the page that asks is the one that brings the visitor back to their work (a menu's link may not)
SIGN_IN_HINT = " (log in from the page that asks for it, not from the menu, so you come back to where you were)"
EMAIL_CODE_HINT = " (the code from the email is email_code)"


def _hint(goal, step):
    """What the browser agent is told after a step's goal, whoever wrote it: which values to type to sign up or to
    type an emailed code, and to sign in from the wall itself. Only the agent reads it; people see the goal."""
    hint = ""
    if step.get("sign_up") and "new_account" not in goal:
        hint += EXISTING_ACCOUNT_HINT if step.get("existing_account") else SIGN_UP_HINT
    if step.get("sign_in_as") and "not from the menu" not in goal:
        hint += SIGN_IN_HINT
    if step.get("open_email") and "email_code" not in goal:
        hint += EMAIL_CODE_HINT
    return hint


def _step_label(step):
    return "Open the email's link" if step.get("open_email") else "Reload the page"


PRODUCTION = "production"
ENVIRONMENTS = (PRODUCTION, "preview", "staging")  # the usual names; a team may call its own anything (qa, uat...)
ENVIRONMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


def environment_name(name):
    """An environment's name as typed ("Staging", "QA 2") -> its name ("staging", "qa-2"); raises SpecError when it
    can't be one. Only production is special: tests that change data never run there."""
    clean = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    if not ENVIRONMENT_NAME.match(clean):
        raise SpecError(f"“{name}” can’t be an environment’s name: use letters, like staging or qa")
    return clean


def kept_off(config, environment):
    """The user types never explored or tested on this environment: on production, those marked `production: false`
    (an admin, until the team allows admin tests there). None elsewhere."""
    if (environment or PRODUCTION) != PRODUCTION:
        return set()
    return {name for name, persona in config.personas.items() if not persona.production}


def without_personas(config, names):
    """The config without these user types: gone from `personas` and from every page-access rule (a rule left with
    nobody in it is dropped)."""
    if not names:
        return config
    access = {}
    for path, rule in config.access.items():
        kept = {k: [p for p in rule.get(k, []) if p not in names] for k in ("allow", "deny")}
        if kept["allow"] or kept["deny"]:
            access[path] = {**rule, **kept}
    return replace(config, personas={n: p for n, p in config.personas.items() if n not in names}, access=access)


KEPT_OFF_REASON = ("an admin kind of user, not explored or tested on production (Settings › Kinds of user: "
                   "Allow admin tests on production)")


def waiting_for_production(data):
    """A change's journey kept as a test, not yet proven on production by a release. A journey kept on a plan
    without the release check ("wait": false) never waits: it runs on every environment like any test."""
    release = data.get("release") or {}
    return bool(release) and not release.get("live") and release.get("wait", True) is not False


def runs_in_environment(data, environment):
    """A test's JSON runs in that environment: its run_on allows it, and on production its change is released."""
    if environment == PRODUCTION and waiting_for_production(data):
        return False
    return not data.get("run_on") or environment in data["run_on"]


def _filled_checks(expect, fill, where):
    """Checks with their templates filled: a string value, or the text of a {"count": {"text": ...}} check."""
    def filled(kind, value):
        if isinstance(value, str):
            return fill(value, f"{where}.{kind}")
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            return {**value, "text": fill(value["text"], f"{where}.{kind}.text")}
        return value

    return tuple({kind: filled(kind, value)} for item in expect for kind, value in item.items())


def parse_flows(raw, *, config=None, base_url=None, site=None, env=None, run_id="", source="<flow>",
                skip_personas=frozenset()):
    """One flow file -> one Flow per persona it runs as. `skip_personas`: kinds of user this run leaves out (an app
    whose tests run in its own CI keeps no test login here: only what needs none runs, see without_logins)."""
    env = os.environ if env is None else env
    site = site_of(base_url, site)
    config = config or Config()
    _validate(raw, "flow.schema.json", source)
    data = _defaults(raw, schema()["properties"])
    flows = []
    if any(step.get("sign_in_as") in skip_personas for step in data.get("steps") or ()):
        return flows  # a visitor who signs in partway as a kind of user left out
    for persona, expect, expect_denied in _variants(data):
        if persona in skip_personas:
            continue
        where = f"{source} (as {persona})" if persona else source
        if not expect and data.get("steps"):
            expect = data["steps"][-1]["expect"]  # a journey's final checks are its last step's
        if not expect:
            raise SpecError(f"{where}: needs `expect` checks")
        values = dict(config.persona(persona, source).values) if persona else {}
        for step in data.get("steps") or ():
            if step.get("sign_in_as"):  # a visitor who signs in partway: that user type's sign-in values
                values.update(config.persona(step["sign_in_as"], source).values)
        values.update(data["values"])
        account, not_run = _new_account(data, env, run_id, where)
        if account:  # a brand-new identity for this run: typed like any value, its password hidden like a secret
            values.update(new_account_email=account["email"], new_account_name=account["name"],
                          new_account_password="{{secrets.%s}}" % NEW_PASSWORD_ENV)
            env = {**env, NEW_PASSWORD_ENV: account["password"]}
            if account.get("state", account_book.RESERVED) != account_book.RESERVED:  # made: log in with it
                data = {**data, "steps": [{**s, "existing_account": True} if s.get("sign_up") else s
                                          for s in data["steps"]]}
        if data["changes_password"]:
            if not persona:
                raise SpecError(f"{where}: changes_password needs the user type whose password it changes (`as`)")
            if not env.get(password_change.env_name(persona)):
                not_run = password_change.not_run_reason(persona, env)
                values = {k: v for k, v in values.items() if password_change.env_name(persona) not in str(v)}
        built = data
        way = (data.get("way_in") or {}).get(persona or "")
        if way and not data.get("steps"):  # it starts where this kind of user starts and clicks its way in (way_in.py)
            from . import way_in

            start, journey = way_in.steps(way, data["goal"], expect,
                                          lambda page: site.template(page) if site else "{{BASE_URL}}" + page)
            built = {**data, "start": start, "goal": None, "steps": journey}
        flow = _build(
            built,
            name=f"{data['name']}@{persona}" if persona else data["name"],
            persona=persona,
            expect=expect,
            expect_denied=expect_denied,
            values_templates=values,
            fill=_Interpolator(site, env, where, run_id),
            source=source,
            acts=bool(data["values"]),  # its own values, not the sign-in's
        )
        if data["changes_password"]:
            flow = replace(flow, changes_password=persona, not_run=not_run)
        elif not_run or account:
            flow = replace(flow, not_run=not_run, inbox=account["inbox"] if account else "")
        flows.append(flow)
    return flows


NEW_PASSWORD_ENV = "EVERYROLE_NEW_ACCOUNT_PASSWORD"  # where a new account's password sits, so it is redacted
NO_INBOX = ("It creates a new account, and this run has no EveryRole inbox for it (only EveryRole's own browsers "
            "have one), so it isn't run here.")


def _new_account(data, env, run_id, where):
    """-> (identity, why it can't run): a journey with a `sign_up` step gets a brand-new identity for this run; an
    `open_email` step or an `email_received` check needs one before it."""
    steps = data.get("steps") or ()
    signs_up = next((n for n, s in enumerate(steps) if s.get("sign_up")), None)
    for n, step in enumerate(steps):
        reads_mail = step.get("open_email") or any("email_received" in c for c in step.get("expect") or ())
        if reads_mail and (signs_up is None or n < signs_up):
            raise SpecError(f"{where}: step {n + 1} reads the new account's email, but no step before it signs up "
                            "(sign_up)")
    if signs_up is None:
        return None, ""
    book = account_book.from_env(env)  # the run's (or the change's) one account per role, made once
    if data.get("account") and book:  # a cleanup: that row's own account, never the role's newest
        account = book.row(data["account"])
        if account is None:
            raise SpecError(f"{where}: no account {data['account']} in this run's account book")
        return account, ""
    mint = lambda: mailbox.new_account(env, run_id, data["name"])  # noqa: E731
    account = book.identity(account_book.NEW_USER, mint) if book else mint()
    return (account, "") if account else (None, NO_INBOX)


def parse_flow(raw, **kwargs):
    """Single-run convenience for flows without personas."""
    flows = parse_flows(raw, **kwargs)
    if len(flows) != 1:
        raise SpecError(f"{kwargs.get('source', '<flow>')}: expands to {len(flows)} runs; use parse_flows")
    return flows[0]


EMAIL_SIGNIN = (" When the page offers to send a sign-in link or code by email (a button like “Email me a link”), "
                "use that with the email value instead of the password; only when it offers none, use the password. "
                "Stop once the page says an email was sent, or once signed in.")
FINISH_SIGNIN = ("If the app still asks something before its own pages (accept or join an invitation, first-time "
                 "questions, a welcome or onboarding screen, terms to accept), go through it: accept or join, and "
                 "answer each question with its first option or skip it. Stop once you are on the app's own pages.")
EMAIL_ONLY_SIGNIN = ("Ask this app to email a sign-in link or code: type the email value in the field of the form that "
                     "sends one (its button says something like “Email me a link” or “Send me a code”), never in a "
                     "password form, and press that button. Stop once the page says the email was sent.")
EMAIL_SIGNIN_WAIT_S = 120


def login_flow(config, persona, *, base_url=None, site=None, env=None, run_id=""):
    """The sign-in flow for one persona, built from the config's `login` recipe and the persona's values. A persona
    with its own sign-in page (`signin`) starts there and is signed in by that page's own checks.

    A persona whose email is one of EveryRole's inboxes (Invite EveryRole) signs in by email when the app offers it:
    it asks for the link or code, the email is opened (only when not signed in yet: a password app needs none), and
    whatever the app still asks (join, accept) is finished. Its password is used only when the page offers no link."""
    env = os.environ if env is None else env
    person = config.persona(persona, config.source or CONFIG_FILE)
    data = {**config.login, **(person.signin or {}), "allowed_hosts": [], "run_on": []}
    fill = _Interpolator(site_of(base_url, site), env, f"{config.source or CONFIG_FILE} (login as {persona})", run_id)
    values, inbox = person.values, inbox_of(person, env, fill)
    by_email = inbox and (person.signin_by == "email" or not _fills(fill, values.get("password", "")))
    if by_email:  # known to sign in by email (or no password at all): ask for the link, open it, finish
        values = {k: v for k, v in values.items() if k != "password"}
        signed_in = data["expect"]
        data = {**data, "goal": None, "steps": [
            {"goal": EMAIL_ONLY_SIGNIN, "expect": [], "checked_by_next": True},
            {"open_email": True, "email_wait_s": EMAIL_SIGNIN_WAIT_S, "expect": signed_in},
            {"goal": FINISH_SIGNIN, "expect": signed_in}]}
    elif inbox and person.signin_by != "password":  # not known yet (accepting an invitation): either way works
        signed_in = data["expect"]
        data = {**data, "goal": None, "steps": [
            {"goal": data["goal"] + EMAIL_SIGNIN, "expect": [], "checked_by_next": True},
            {"open_email": True, "email_if_needed": True, "email_wait_s": EMAIL_SIGNIN_WAIT_S, "expect": signed_in},
            {"goal": FINISH_SIGNIN, "expect": signed_in}]}
    flow = _build(
        data,
        name=f"login@{persona}",
        persona=persona,
        expect=data["expect"],
        expect_denied=False,
        values_templates=values,
        fill=fill,
        source=config.source or CONFIG_FILE,
        is_login=True,
    )
    return replace(flow, inbox=inbox) if inbox else flow


def inbox_of(person, env, fill):
    """The inbox folder of a persona whose email is one of EveryRole's inbox addresses, else "" (no inbox on this run,
    or an ordinary test account)."""
    from . import mailbox

    domain, key, folder = (env.get(n, "") for n in (mailbox.DOMAIN_ENV, mailbox.KEY_ENV, mailbox.DIR_ENV))
    template = person.values.get("username_or_email") or person.values.get("email") or ""
    if not (domain and key and folder and template):
        return ""
    try:
        email = fill(template, "values.username_or_email")
    except SpecError:
        return ""
    found = mailbox.owner(domain, key, email)
    return str(Path(folder) / found[1]) if found else ""


def _fills(fill, template):
    try:
        fill(template, "values")
        return True
    except SpecError:
        return False


def signin_starts(config, persona, *, base_url=None, site=None, env=None):
    """The addresses where this user type signs in: the app's sign-in page (`login.start`) and its own one
    (`signin.start`), when set. One that can't be filled in (no address given) is left out."""
    env = os.environ if env is None else env
    person = config.personas.get(persona)
    starts = [config.login.get("start"), (person.signin or {}).get("start") if person else None]
    fill = _Interpolator(site_of(base_url, site), env, f"{config.source or CONFIG_FILE} (sign-in of {persona})")
    found = []
    for start in filter(None, starts):
        try:
            found.append(fill(start, "start"))
        except SpecError:
            continue
    return list(dict.fromkeys(found))


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None


def load_flow_file(path, *, config=None, base_url=None, site=None, env=None, run_id="", skip_personas=frozenset()):
    return parse_flows(_read_json(path), config=config, base_url=base_url, site=site, env=env, run_id=run_id,
                       source=str(path), skip_personas=skip_personas)


def load_flow(path, **kwargs):
    flows = load_flow_file(path, **kwargs)
    if len(flows) != 1:
        raise SpecError(f"{path}: expands to {len(flows)} runs; use load_flow_file")
    return flows[0]


def flow_files(paths):
    files = []
    for path in map(Path, paths):
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    if not files:
        raise SpecError(f"No flow files found in {', '.join(map(str, paths))}")
    return files


def load_flows(paths, *, config=None, base_url=None, site=None, env=None, run_id="", skip_personas=frozenset()):
    flows = []
    for path in flow_files(paths):
        flows.extend(load_flow_file(path, config=config, base_url=base_url, site=site, env=env, run_id=run_id,
                                    skip_personas=skip_personas))
    seen = {}
    for flow in flows:
        if flow.name in seen:
            raise SpecError(f"Duplicate flow name {flow.name!r} in {seen[flow.name]} and {flow.source}")
        seen[flow.name] = flow.source
    return flows


def without_logins(config):
    """(config, the kinds of user left out) for a run that has no test login: an app whose tests run in its own CI
    (its logins live only there). Kinds of user that sign in leave the run with their page checks; visitors (and the
    tests that sign up a brand-new user with EveryRole's own inbox) stay."""
    skipped = frozenset(name for name, persona in config.personas.items() if persona.login)
    return without_personas(config, skipped), skipped
