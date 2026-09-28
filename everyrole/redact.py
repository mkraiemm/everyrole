"""Secret redaction. Secret literals are replaced by `[secret:name]` everywhere they could leave the process."""

import json


class Redactor:
    def __init__(self, secrets=None):
        # Longest first, so a secret containing another secret is replaced whole.
        self._secrets = sorted(
            ((name, value) for name, value in (secrets or {}).items() if value),
            key=lambda item: len(item[1]),
            reverse=True,
        )

    @property
    def active(self):
        return bool(self._secrets)

    def apply(self, text):
        if not isinstance(text, str) or not self._secrets:
            return text
        for name, value in self._secrets:
            placeholder = f"[secret:{name}]"
            text = text.replace(value, placeholder)
            # The same literal as it appears inside serialized JSON (quotes, backslashes, unicode escapes).
            for encoded in {json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}:
                text = text.replace(encoded, placeholder)
        return text

    def scrub(self, data):
        """Redact every string inside a JSON-like structure."""
        if isinstance(data, str):
            return self.apply(data)
        if isinstance(data, dict):
            return {self.apply(k): self.scrub(v) for k, v in data.items()}
        if isinstance(data, (list, tuple)):
            return [self.scrub(v) for v in data]
        return data
