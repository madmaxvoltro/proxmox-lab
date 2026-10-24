"""Host filesystem and network access that is not a Proxmox CLI call.

Same idea as runner.py: one seam, with dry-run and test doubles. Reads are always real;
writes and downloads are skipped (and recorded) in dry-run."""

import hashlib
import json
import logging
import os
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from .errors import LabError

log = logging.getLogger("lab.hostio")
USER_AGENT = "project-lab/0"


class FetchError(LabError):
    pass


class HostIO:
    # reads ---------------------------------------------------------------
    def fetch_text(self, url: str, timeout: float = 30) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except OSError as e:
            raise FetchError(f"cannot fetch {url}: {e}") from e

    def url_exists(self, url: str, timeout: float = 15) -> bool:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return 200 <= r.status < 300
        except OSError:
            return False

    def post(self, url: str, *, form: dict | None = None, json_body: dict | None = None,
             headers: dict | None = None, timeout: float = 30) -> dict:
        """POST and parse a JSON reply. Bodies and replies may hold secrets: never logged."""
        if form is not None:
            body, ctype = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        else:
            body, ctype = json.dumps(json_body or {}).encode(), "application/json"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"User-Agent": USER_AGENT, "Content-Type": ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except OSError as e:  # URLError/HTTPError: message only, never the request body
            raise FetchError(f"POST {url} failed: {e}") from e
        except json.JSONDecodeError as e:
            raise FetchError(f"POST {url} returned invalid JSON") from e

    def read_text(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def listdir(self, path: str) -> list:
        return sorted(os.listdir(path))

    def mode(self, path: str) -> int:
        return os.stat(path).st_mode & 0o777

    def owner_uid(self, path: str) -> int:
        return os.stat(path).st_uid

    def free_bytes(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def same_device_as_root(self, path: str) -> bool:
        """True if `path` lives on the same filesystem as / (i.e. the disk is not actually mounted)."""
        root = os.path.abspath(os.sep)
        return os.stat(path).st_dev == os.stat(root).st_dev

    def sha256(self, path: str, algo: str = "sha256") -> str:
        h = hashlib.new(algo)
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # writes --------------------------------------------------------------
    def makedirs(self, path: str, mode: int = 0o750) -> None:
        os.makedirs(path, mode=mode, exist_ok=True)

    def write_text(self, path: str, text: str, mode: int = 0o644) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def fetch_file(self, url: str, dest: str, timeout: float = 60) -> None:
        """Download to dest via a .part file so a failed download never looks complete."""
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        part = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
        except OSError as e:
            if os.path.exists(part):
                os.unlink(part)
            raise FetchError(f"download failed for {url}: {e}") from e
        os.replace(part, dest)

    def remove(self, path: str) -> None:
        if os.path.exists(path):
            os.unlink(path)


class DryRunHostIO(HostIO):
    def __init__(self):
        self.skipped: list = []

    def _skip(self, what, *args):
        self.skipped.append((what, *args))
        log.info("[dry-run] %s %s", what, " ".join(str(a) for a in args))

    def makedirs(self, path, mode=0o750):
        self._skip("mkdir", path)

    def write_text(self, path, text, mode=0o644):
        self._skip("write", path)

    def fetch_file(self, url, dest, timeout=60):
        self._skip("download", url, dest)

    def remove(self, path):
        self._skip("remove", path)

    def post(self, url, **kw):
        self._skip("POST", url)
        return {"key": "tskey-dry-run"}
