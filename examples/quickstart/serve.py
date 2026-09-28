"""Serve the demo app in `fixtures/` for the quickstart: python examples/quickstart/serve.py [port]

A plain `python -m http.server` on the fixtures folder, bound to 127.0.0.1. Press Ctrl+C to stop.
"""

import functools
import http.server
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else 8794
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Demo app at http://127.0.0.1:{port}/index.html (serving {FIXTURES})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
