"""One app, several addresses: the parts of a site and where each part lives in the environment being tested.

An app is often spread over more than one address: a public site on example.com, the app itself on
app.example.com, docs on docs.example.com. Each address is a *part* with a name. The first part is the main one.

A page is written with its part: `/dashboard` is a page of the main part, `site:/privacy` a page of the part
named `site`. The same page names hold in every environment; only the addresses change. In the config:

  "environments": {"production": {"app": "https://app.example.com", "site": "https://example.com"},
                   "staging":    {"app": "https://staging-app.example.com", "site": "https://staging.example.com"}}

A config without `environments` is a one-part site whose address is given on the command line (--base-url).
"""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import SpecError

PART_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


PAGE = re.compile(r"^([a-z][a-z0-9_]*):(/(?!/).*)$")  # "site:/privacy" (never "https://...")


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
        """The page a full address is, written with its part ("site:/privacy"), or None outside the site."""
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"}:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        for name, base in self.parts:
            prefix = urlparse(base).path.rstrip("/")
            if _origin(base) == origin and (parsed.path or "/").startswith(prefix):
                path = (parsed.path or "/")[len(prefix):] or "/"
                return join("" if name == self.main else name, path if path.startswith("/") else "/" + path)
        return None

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
