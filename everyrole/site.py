"""One app, several addresses: the parts of a site and where each part lives in the environment being tested.

An app is often spread over more than one address: a public site on example.com, the app itself on
app.example.com, docs on docs.example.com. Each address is a *part* with a name. The first part is the main one.

A page is written with its part: `/dashboard` is a page of the main part, `site:/privacy` a page of the part
named `site`. The same page names hold in every environment; only the addresses change. In the config:

  "environments": {"production": {"app": "https://app.example.com", "site": "https://example.com"},
                   "staging":    {"app": "https://staging-app.example.com", "site": "https://staging.example.com"}}

A config without `environments` is a one-part site whose address is given on the command line (--base-url).

Single-page apps that route after a `#` (`https://app.io/#/login`, `#!/settings`) have their pages there: the part
after `#` is kept when it is a route (it starts with `/`, or `!/`), written as `/#/login`. Any other `#...` is a place
on the same page (`#pricing`) and is dropped, as before. Nothing about the app is assumed: it is what its own
addresses say.
"""

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urlparse

from .errors import SpecError

PART_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
IP = re.compile(r"^[\d.]+$|:")
PAGE = re.compile(r"^([a-z][a-z0-9_]*):(/(?!/).*)$")  # "site:/privacy" (never "https://...")
HASH_ROUTE = re.compile(r"^!?/")  # what follows "#" in a single-page app's own addresses: "/login", "!/settings"


def hash_route(url):
    """The route a single-page app keeps after `#` ("#/login"), or "" (no `#`, or a place on the page)."""
    fragment = urldefrag(url or "")[1]
    return f"#{fragment}" if HASH_ROUTE.match(fragment) else ""


def without_anchor(url):
    """An address without a place on its page (`#pricing`), keeping a single-page app's route (`#/login`): two
    addresses that differ only by an anchor are the same page; two hash routes are two pages."""
    return urldefrag(url or "")[0] + hash_route(url)


def split(page):
    """"site:/privacy" -> ("site", "/privacy"); "/pricing" -> ("", "/pricing")."""
    match = PAGE.match(page or "")
    return (match.group(1), match.group(2)) if match else ("", page)


def join(part, path):
    return f"{part}:{path}" if part else path


def _origin(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


@dataclass(frozen=True)
class Site:
    parts: tuple  # ((name, base_url), ...): the first part is the main one

    @classmethod
    def single(cls, base_url, name="main"):
        return cls(((name, base_url.rstrip("/")),))

    @classmethod
    def of(cls, parts):
        """From an ordered {name: base_url} mapping, validated: names, http(s) addresses, one origin per part."""
        items = [(str(n), str(u).rstrip("/")) for n, u in parts.items()]
        if not items:
            raise SpecError("a site needs at least one address")
        seen = {}
        for name, url in items:
            if not PART_NAME.match(name):
                raise SpecError(f"part name {name!r} must be lowercase letters, digits or _ (e.g. app, site)")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise SpecError(f"part {name!r} needs a full address like https://app.example.com, got {url!r}")
            if _origin(url) in seen:
                raise SpecError(f"parts {seen[_origin(url)]!r} and {name!r} have the same address {url}")
            seen[_origin(url)] = name
        return cls(tuple(items))

    # ------------------------------------------------------------------ the parts

    @property
    def main(self):
        return self.parts[0][0]

    @property
    def main_url(self):
        return self.parts[0][1]

    @property
    def names(self):
        return [name for name, _ in self.parts]

    @property
    def hosts(self):
        return tuple(dict.fromkeys(urlparse(url).hostname for _, url in self.parts))

    def url_of(self, part):
        for name, url in self.parts:
            if name == (part or self.main):
                return url
        raise SpecError(f"unknown part {part!r} (parts: {', '.join(self.names)})")

    # ------------------------------------------------------------------ pages <-> addresses

    def url(self, page):
        """"site:/privacy" -> "https://example.com/privacy"; "/x" -> the main part's address + "/x"."""
        part, path = split(page)
        return self.url_of(part) + path

    def page(self, url):
        """The page a full address is, written with its part ("site:/privacy"), or None outside the site. A
        single-page app's route after `#` is part of the page ("/#/login")."""
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"}:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        for name, base in self.parts:
            prefix = urlparse(base).path.rstrip("/")
            if _origin(base) == origin and (parsed.path or "/").startswith(prefix):
                path = (parsed.path or "/")[len(prefix):] or "/"
                path = (path if path.startswith("/") else "/" + path) + hash_route(url)
                return join("" if name == self.main else name, path)
        return None

    def address(self, url):
        """The page a full address is, with its query ("/login?mode=signup"): what the page shows can depend on it
        (one sign-in page opening on its sign-up panel), so a click is told apart by it. None outside the site."""
        page = self.page(url)
        query = urlparse(url or "").query
        if page is None or not query:
            return page
        before, mark, route = page.partition("#")  # the query comes before a hash route: "/?ref=1#/login"
        return f"{before}?{query}{mark}{route}"

    def template(self, page):
        """A page as a flow's start template: "{{BASE_URL}}/x" or "{{URL.site}}/privacy"."""
        part, path = split(page)
        if part and part != self.main:
            self.url_of(part)
            return "{{URL.%s}}%s" % (part, path)
        return "{{BASE_URL}}" + path

    def label(self, page):
        """How a page reads for people: with the part's host when the site has more than one part."""
        part, path = split(page)
        if len(self.parts) == 1 or not part:
            return path if len(self.parts) == 1 else f"{urlparse(self.main_url).netloc}{path}"
        return f"{urlparse(self.url_of(part)).netloc}{path}"

    def as_args(self):
        """Command-line form for a worker: --base-url main, --part name=url for the others."""
        args = ["--base-url", self.main_url]
        for name, url in self.parts[1:]:
            args += ["--part", f"{name}={url}"]
        return args


def resolve(config, *, base_url=None, environment=None, parts=()):
    """The site for one run: the environment's addresses from the config, `base_url` for the main part (e.g. a
    preview deploy), and `parts` ("name=url") for any other part. Every part must end up with an address."""
    kit = getattr(config, "environments", None) or {}
    names = list(next(iter(kit.values()))) if kit else []
    addresses = dict(kit.get(environment, {})) if environment else {}
    main = names[0] if names else "main"
    if base_url:
        addresses = {main: base_url, **{k: v for k, v in addresses.items() if k != main}}
    for item in parts:
        name, _, url = str(item).partition("=")
        if not url:
            raise SpecError(f"--part needs name=address, got {item!r}")
        if names and name not in names:
            raise SpecError(f"--part {name!r} is not a part of this app (parts: {', '.join(names)})")
        addresses[name] = url
    if not addresses:
        where = f"environment {environment!r} is not in the config" if environment and kit else "no address given"
        raise SpecError(f"Which address should be tested? {where}: pass --base-url")
    ordered = {n: addresses[n] for n in (names or [main]) if n in addresses}
    ordered.update({n: u for n, u in addresses.items() if n not in ordered})
    missing = [n for n in names if n not in ordered]
    if missing:
        raise SpecError(f"no address for part(s) {', '.join(missing)} in environment {environment!r}: add them to "
                        f"`environments` in the config, or pass --part {missing[0]}=https://...")
    return Site.of(ordered)


def siblings(site, hosts):
    """Hosts seen in links that look like other parts of the same app: same parent domain as a part, e.g.
    example.com next to app.example.com. Only suggestions, from links actually seen; people confirm them."""
    known = set(site.hosts)
    found = []
    for host in hosts:
        host = (host or "").lower()
        if not host or host in known or host in found or "." not in host:
            continue
        if any(_related(host, mine) for mine in known):
            found.append(host)
    return sorted(found)


def _related(a, b):
    if IP.match(a) or IP.match(b):
        return False  # numbered addresses have no parent domain
    if a.endswith("." + b) or b.endswith("." + a):
        return True
    parent_a, parent_b = a.partition(".")[2], b.partition(".")[2]
    return parent_a == parent_b and "." in parent_a


def part_name(host, taken):
    """A short name for a part from its host: app.example.com -> app, example.com -> example."""
    labels = [x for x in (host or "").lower().split(".") if x]
    if labels[:1] == ["www"] and len(labels) > 2:
        labels = labels[1:]  # www.example.com is "example"
    name = re.sub(r"[^a-z0-9]+", "_", labels[0] if labels else "").strip("_") or "part"
    name = name if name[0].isalpha() else f"p_{name}"
    unique, n = name, 2
    while unique in taken:
        unique, n = f"{name}_{n}", n + 1
    return unique
