"""What a page works out from a file, kept while the file stays the same.

An app's pages read the same files on every visit: its tests (flows/*.json, read a dozen times per page), each run's
results.json (for how long each test takes) and each exploration's archive (explorations/<job id>.json, half a MB to
several MB with every page it saw). Reading and parsing them again on every visit made those pages slow.

A memo keeps, per file, what was worked out from it (a small extract, never the whole run or archive), keyed by the
file's path, size and modification time: a file written again is read again. The values are shared between visits:
callers never change them (Store.flows keeps the text and parses it for each caller).
"""

import threading
from collections import OrderedDict
from pathlib import Path


class FileMemo:
    def __init__(self, derive, size):
        """`derive(path)` -> what to keep for a file (it may raise OSError/ValueError: nothing is kept then).
        `size`: how many files are kept (the least recently used one goes first)."""
        self.derive = derive
        self.size = size
        self._kept = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path, facts=None):
        """`facts`: the file's os.stat_result when the caller has it already (os.scandir)."""
        path = Path(path)
        facts = facts or path.stat()  # OSError for a missing file, like reading it
        key = (str(path), facts.st_mtime_ns, facts.st_size)
        with self._lock:
            if key in self._kept:
                self._kept.move_to_end(key)
                return self._kept[key]
        value = self.derive(path)
        with self._lock:
            self._kept[key] = value
            self._kept.move_to_end(key)
            while len(self._kept) > self.size:
                self._kept.popitem(last=False)
        return value

    def clear(self):
        with self._lock:
            self._kept.clear()
