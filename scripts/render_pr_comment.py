"""Render a report's summary.md (the exact PR comment body) as a GitHub-style comment, then photograph it.

Usage: python scripts/render_pr_comment.py <report-dir> <out.png> [--width 900]

Starts its own throwaway headless Chrome (EveryRole's `chrome.py`: free port, temporary profile, stopped at the
end). Needs the `markdown` package: pip install -e ".[docs]".
"""

import argparse
import base64
import html
import json
import tempfile
import urllib.request
from pathlib import Path

import markdown
from websockets.sync.client import connect

from everyrole.chrome import chrome_session

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<style>
  body {{ margin: 0; background: #f6f8fa; font: 14px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1f2328; padding: 16px; }}
  .comment {{ max-width: {width}px; margin: 0 auto; background: #fff; border: 1px solid #d1d9e0; border-radius: 6px; }}
  .head {{ display: flex; align-items: center; gap: 8px; padding: 8px 16px; background: #f6f8fa;
           border-bottom: 1px solid #d1d9e0; border-radius: 6px 6px 0 0; color: #59636e; font-size: 13px; }}
  .head b {{ color: #1f2328; }}
  .bot {{ border: 1px solid #d1d9e0; border-radius: 2em; padding: 0 6px; font-size: 11px; }}
  .body {{ padding: 16px; }}
  h3 {{ margin: 0 0 8px; font-size: 18px; }}
  table {{ border-collapse: collapse; margin: 12px 0; }}
  th, td {{ border: 1px solid #d1d9e0; padding: 6px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f6f8fa; }}
  code {{ background: #eff1f3; border-radius: 6px; padding: .2em .4em; font-size: 85%; }}
  blockquote {{ margin: 8px 0; padding: 0 1em; color: #59636e; border-left: .25em solid #d1d9e0; }}
</style><title>{title}</title></head><body>
<div class="comment">
  <div class="head"><b>EveryRole</b><span class="bot">bot</span> commented</div>
  <div class="body">{body}</div>
</div></body></html>"""


def page_html(summary_md, *, width, title):
    body = markdown.markdown(summary_md, extensions=["tables", "md_in_html"])
    return TEMPLATE.format(body=body, width=width, title=html.escape(title))


class Tab:
    """The smallest DevTools client that can load a page and take a full-page screenshot."""

    def __init__(self, websocket_url):
        self._ws = connect(websocket_url, max_size=None)
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def close(self):
        self._ws.close()


def screenshot(html_path, out, *, width):
    with chrome_session() as chrome:
        with urllib.request.urlopen(f"{chrome.url}/json/list", timeout=10) as response:
            page = next(t for t in json.load(response) if t["type"] == "page")
        tab = Tab(page["webSocketDebuggerUrl"])
        try:
            tab.call("Page.enable")
            tab.call("Emulation.setDeviceMetricsOverride", width=width + 32, height=600, deviceScaleFactor=2,
                     mobile=False)
            tab.call("Page.navigate", url=Path(html_path).resolve().as_uri())
            for _ in range(100):  # wait for the load, up to ~10 s
                if tab.call("Runtime.evaluate", expression="document.readyState")["result"]["value"] == "complete":
                    break
                tab.call("Runtime.evaluate", expression="new Promise(r => setTimeout(r, 100))", awaitPromise=True)
            size = tab.call("Page.getLayoutMetrics")["cssContentSize"]
            bottom = tab.call("Runtime.evaluate", returnByValue=True,
                              expression="document.querySelector('.comment').getBoundingClientRect().bottom")
            clip = {"x": 0, "y": 0, "width": size["width"], "height": bottom["result"]["value"] + 16, "scale": 1}
            data = tab.call("Page.captureScreenshot", format="png", clip=clip, captureBeyondViewport=True)["data"]
        finally:
            tab.close()
    Path(out).write_bytes(base64.b64decode(data))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report_dir")
    parser.add_argument("out")
    parser.add_argument("--width", type=int, default=900)
    args = parser.parse_args()
    summary = (Path(args.report_dir) / "summary.md").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="everyrole-comment-") as tmp:
        page = Path(tmp) / "comment.html"
        page.write_text(page_html(summary, width=args.width, title="EveryRole PR comment"), encoding="utf-8")
        print(screenshot(page, args.out, width=args.width))


if __name__ == "__main__":
    main()
