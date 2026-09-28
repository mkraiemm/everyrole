"""Flow and config files: schema validation, schema defaults, persona expansion, template interpolation.

Templates: `{{BASE_URL}}`, `{{BASE_HOST}}` (the main address being tested), `{{URL.<part>}}` (the address of
another part of the app, see site.py), `{{RUN_ID}}` (unique per suite run) and `{{secrets.NAME}}` (env var NAME).
Every secret a flow uses is tracked so it can be redacted everywhere.
"""

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

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
    source: str = ""
    persona: str = ""  # "" = no persona (fresh signed-out browser)
    expect_denied: bool = False
    block_writes: bool = False
    ignore_saves: tuple = ()
    is_login: bool = False

    def runs_in(self, environment):
        return not self.run_on or environment in self.run_on


@dataclass(frozen=True)
class Persona:
    name: str
    values: dict  # templates, interpolated per flow so secrets are tracked per flow
    login: bool = True
    saved_signin: dict = None  # {"file", "check"}: a sign-in a person did by hand, reused instead of typing


@dataclass(frozen=True)
class Config:
    login: dict = field(default_factory=dict)
    personas: dict = field(default_factory=dict)  # name -> Persona
    access: dict = field(default_factory=dict)  # path -> {"allow": [...], "deny": [...]}
    denied_when: tuple = ()
    access_settle_s: int = 0
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
        if not path.is_file():
            props = schema("config.schema.json")["properties"]
            return Config(run=_defaults({}, props["run"]["properties"]))
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
                                 saved_signin=merged.get("saved_signin"))
    login = _defaults(data["login"], props["login"]["properties"]) if "login" in data else {}
    if any(p.login and not p.saved_signin for p in personas.values()) and not login:
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


def _build(data, *, name, persona, expect, expect_denied, values_templates, fill, source, is_login=False):
    """`source` is the file path (used to re-load the flow); `fill.source` is the label used in errors."""
    values = {k: fill(v, f"values.{k}") for k, v in values_templates.items()}
    start = fill(data["start"], "start")
    if urlparse(start).scheme not in {"http", "https"}:
        raise SpecError(f"{fill.source}: start must be an http(s) URL, got {start!r}")
    checks = tuple({kind: fill(value, f"expect.{kind}") if isinstance(value, str) else value}
                   for item in expect for kind, value in item.items())
    # by default a flow may go anywhere in the app (every part), never outside it
    default_hosts = (*(fill.site.hosts if fill.site else ()), urlparse(start).hostname)
    allowed = tuple(fill(h, "allowed_hosts") for h in data.get("allowed_hosts", ())) or tuple(
        dict.fromkeys(default_hosts))
    goal = fill(data["goal"], "goal")
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
        source=source,
        persona=persona,
        expect_denied=expect_denied,
        block_writes=bool(data.get("block_writes", False)),
        ignore_saves=tuple(data.get("ignore_saves", ())),
        is_login=is_login,
    )


def parse_flows(raw, *, config=None, base_url=None, site=None, env=None, run_id="", source="<flow>"):
    """One flow file -> one Flow per persona it runs as."""
    env = os.environ if env is None else env
    site = site_of(base_url, site)
    config = config or Config()
    _validate(raw, "flow.schema.json", source)
    data = _defaults(raw, schema()["properties"])
    flows = []
    for persona, expect, expect_denied in _variants(data):
        where = f"{source} (as {persona})" if persona else source
        if not expect:
            raise SpecError(f"{where}: needs `expect` checks")
        values = dict(config.persona(persona, source).values) if persona else {}
        values.update(data["values"])
        flows.append(
            _build(
                data,
                name=f"{data['name']}@{persona}" if persona else data["name"],
                persona=persona,
                expect=expect,
                expect_denied=expect_denied,
                values_templates=values,
                fill=_Interpolator(site, env, where, run_id),
                source=source,
            )
        )
    return flows


def parse_flow(raw, **kwargs):
    """Single-run convenience for flows without personas."""
    flows = parse_flows(raw, **kwargs)
    if len(flows) != 1:
        raise SpecError(f"{kwargs.get('source', '<flow>')}: expands to {len(flows)} runs; use parse_flows")
    return flows[0]


def login_flow(config, persona, *, base_url=None, site=None, env=None, run_id=""):
    """The sign-in flow for one persona, built from the config's `login` recipe and the persona's values."""
    env = os.environ if env is None else env
    person = config.persona(persona, config.source or CONFIG_FILE)
    data = {**config.login, "allowed_hosts": [], "run_on": []}
    return _build(
        data,
        name=f"login@{persona}",
        persona=persona,
        expect=config.login["expect"],
        expect_denied=False,
        values_templates=person.values,
        fill=_Interpolator(site_of(base_url, site), env, f"{config.source or CONFIG_FILE} (login as {persona})",
                           run_id),
        source=config.source or CONFIG_FILE,
        is_login=True,
    )


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None


def load_flow_file(path, *, config=None, base_url=None, site=None, env=None, run_id=""):
    return parse_flows(_read_json(path), config=config, base_url=base_url, site=site, env=env, run_id=run_id,
                       source=str(path))


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


def load_flows(paths, *, config=None, base_url=None, site=None, env=None, run_id=""):
    flows = []
    for path in flow_files(paths):
        flows.extend(load_flow_file(path, config=config, base_url=base_url, site=site, env=env, run_id=run_id))
    seen = {}
    for flow in flows:
        if flow.name in seen:
            raise SpecError(f"Duplicate flow name {flow.name!r} in {seen[flow.name]} and {flow.source}")
        seen[flow.name] = flow.source
    return flows
