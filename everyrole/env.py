"""Load KEY=VALUE lines from a .env file without overriding variables already set (CI secrets win).

Model settings set to an empty string count as unset: CI systems pass an unset variable (e.g. a GitHub
`vars.TYPESAFE_URL` nobody defined) as "", which would otherwise hide the .env value and the built-in default.
"""

import os
from pathlib import Path

MODEL_SETTINGS = ("TYPESAFE_URL", "TYPESAFE_MODEL", "TEXT_MODEL_API_KEY", "TEXT_MODEL_BASE_URL", "TEXT_MODEL",
                  "TEXT_MODEL_REASONING")


def load_dotenv(path=".env", environ=None):
    environ = os.environ if environ is None else environ
    for name in MODEL_SETTINGS:
        if environ.get(name) == "":
            del environ[name]
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded
