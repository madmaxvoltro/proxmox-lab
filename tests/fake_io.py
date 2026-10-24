"""In-memory HostIO: no disk, no network."""

import hashlib

from lab.hostio import FetchError, HostIO


class FakeIO(HostIO):
    def __init__(self):
        self.files = {}  # path -> bytes
        self.dirs = {"/mnt/hdd": ["media", "backups"]}  # path -> entries
        self.urls = {}  # url -> text or bytes
        self.modes = {}
        self.root_device_paths = set()  # paths that are on the root filesystem
        self.made = []
        self.removed = []

    def fetch_text(self, url, timeout=30):
        if url not in self.urls:
            raise FetchError(f"cannot fetch {url}: not found")
        v = self.urls[url]
        return v if isinstance(v, str) else v.decode()

    def url_exists(self, url, timeout=15):
        return url in self.urls

    def fetch_file(self, url, dest, timeout=60):
        if url not in self.urls:
            raise FetchError(f"download failed for {url}")
        v = self.urls[url]
        self.files[dest] = v if isinstance(v, bytes) else v.encode()

    def exists(self, path):
        return path in self.files or path in self.dirs

    def listdir(self, path):
        return list(self.dirs.get(path, []))

    def mode(self, path):
        return self.modes.get(path, 0o600)

    def free_bytes(self, path):
        return 1 << 40

    def same_device_as_root(self, path):
        return path in self.root_device_paths

    def sha256(self, path, algo="sha256"):
        return hashlib.new(algo, self.files[path]).hexdigest()

    def makedirs(self, path, mode=0o750):
        self.made.append(path)
        self.dirs.setdefault(path, [])

    def write_text(self, path, text, mode=0o644):
        self.files[path] = text.encode()

    def remove(self, path):
        self.removed.append(path)
        self.files.pop(path, None)
