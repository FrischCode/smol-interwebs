"""The pinned kiwix-serve binary — the "run this and get search" half of the promise.

A default config has no `exe`, and the console must not make a stranger hunt for a
binary: SIW fetches a pinned kiwix-tools release, verifies it by sha256, extracts it
into the machine's tools directory, and points the engine there. The pin is a
repository constant — URL, sha256, size — a decision verifiable offline, bumped by
editing one constant and re-verifying.

Pinned to the kiwix-tools x86_64 release on master.download.kiwix.org.

Verification policy is the queue's: the archive lands as a `.part`, is hash-checked
in full, and only then extracted; an artifact that fails the hash is deleted. An
unverified binary is never the one that gets spawned.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

# The pin. Bumping it: download the new archive, verify it by hand, replace these
# values and the version, and let the test suite re-check the hash and the
# machine type. The windows pin is the x86_64 build on purpose: the 32-bit build has a
# query-time address-space ceiling (a book >= 10 GiB 500s any query co-mapping it, measured), which is a search-space failure, not a startup failure — so the
# fetch verifies the binary is 64-bit the way it verifies the hash.
RELEASE: dict = {
    "version": "3.8.1",
    "url": "https://master.download.kiwix.org/release/kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip",
    "sha256": "fcd01ed2b93e9a68632c7863c83b9f66bf64406a66357be1df7b8b75596f3e45",
    "size": 18_301_924,
    "binary": "kiwix-serve.exe",
    "machine": 0x8664,  # PE machine type: AMD64. A 32-bit archive is a failed fetch.
}

CHUNK = 1_048_576  # 1 MiB read/hash chunks


def default_tools_dir() -> Path:
    """Where the fetched engine lives: the machine data dir, not the repo."""
    from siw import config as config_module

    return config_module.machine_config_dir() / "tools" / "kiwix-tools"


def _default_downloader(url: str, dest: Path, progress: callable) -> None:
    """Stream `url` to `dest` (a `.part` path). Raises on any HTTP/transport error;
    a partial file is the caller's to clean up."""
    req = urllib.request.Request(url, headers={"User-Agent": "siw/0.2 (console)"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            progress(got, total)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class ToolsFetcher:
    """One pinned binary, one machine dir, one worker thread.

    States: `missing` (never fetched) → `fetching` → `verifying` → `extracting` →
    `ok`, or `failed` from any of the three (a failed fetch keeps its error and its
    part file is gone; `start()` retries). `snapshot()` answers the wire at any time;
    nothing here blocks a poll."""

    def __init__(self, tools_dir: str | Path, downloader: callable | None = None):
        self._dir = Path(tools_dir)
        self._part = self._dir.parent / f"kiwix-tools-{RELEASE['version']}.zip.part"
        self._marker = self._dir / ".siw-verified"
        self._downloader = downloader or _default_downloader
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = "missing"
        self._error: str | None = None
        self._received = 0
        self._on_ready: callable | None = None
        self._adopt()

    def set_on_ready(self, callback: callable) -> None:
        """Called once, from the worker, when a fetch lands `ok` (or `failed`)."""
        with self._lock:
            self._on_ready = callback

    def _adopt(self) -> None:
        """A verified install from a previous run is `ok` without touching the net --
        verified again here. The marker records the binary's own sha256 and adoption
        re-hashes the file: a marker is a promise, the bytes are the fact."""
        try:
            mark = json.loads(self._marker.read_text(encoding="utf-8"))
            binary = self._dir / RELEASE["binary"]
            if (mark.get("version") == RELEASE["version"]
                    and mark.get("sha256") == RELEASE["sha256"]
                    and binary.is_file()
                    and mark.get("binary_sha256") == _sha256_of(binary)):
                self._state = "ok"
                return
        except (OSError, json.JSONDecodeError):
            pass
        self._state = "missing"

    def snapshot(self) -> dict:
        with self._lock:
            in_flight = self._state in ("fetching", "verifying", "extracting")
            return {
                "state": self._state,
                "version": RELEASE["version"],
                "path": str(self._dir / RELEASE["binary"]) if self._state == "ok" else None,
                "exists": (self._dir / RELEASE["binary"]).is_file() if self._state == "ok" else False,
                "progress": ({"received": self._received, "total": int(RELEASE["size"])}
                             if in_flight else None),
                "error": self._error if self._state == "failed" else None,
            }

    def start(self) -> None:
        """Idempotent: a running fetch or a verified install is left alone."""
        with self._lock:
            if self._state in ("ok",) or (self._thread is not None and self._thread.is_alive()):
                return
            self._state = "fetching"
            self._error = None
            self._received = 0
            self._thread = threading.Thread(target=self._run, name="siw-tools-fetch", daemon=True)
            self._thread.start()

    def _progress(self, got: int, total: int) -> None:
        with self._lock:
            self._received = got
            if total:
                self._total_override = total

    def _run(self) -> None:
        cb = None
        try:
            self._download()
            self._verify()
            self._extract()
            with self._lock:
                self._state = "ok"
                self._error = None
                cb = self._on_ready
        except Exception as exc:  # the worker must die with a state, not a stack
            with self._lock:
                self._state = "failed"
                self._error = f"{exc.__class__.__name__}: {exc}"
                cb = self._on_ready
                self._remove_part()
        if cb:
            cb()

    def _remove_part(self) -> None:
        try:
            self._part.unlink()
        except FileNotFoundError:
            pass

    def _download(self) -> None:
        self._part.parent.mkdir(parents=True, exist_ok=True)
        self._downloader(RELEASE["url"], self._part, self._progress)

    def _verify(self) -> None:
        with self._lock:
            self._state = "verifying"
        size = self._part.stat().st_size
        if size != RELEASE["size"]:
            # Cheap refusal before the hash: a truncated download says so by its length.
            raise OSError(f"size mismatch — the download is {size} bytes, "
                          f"the pinned release says {RELEASE['size']}")
        got = _sha256_of(self._part)
        if got != RELEASE["sha256"]:
            raise OSError(f"sha256 mismatch — got {got[:12]}…, the pin wants {RELEASE['sha256'][:12]}…")
        _verify_binary_pe(self._part)


    def _extract(self) -> None:
        with self._lock:
            self._state = "extracting"
        self._dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self._part) as zf:
            # The stdlib filter is the check wherever the interpreter has it (3.12+):
            # unlike the hand-rolled one it refuses the backslash spellings and every
            # symlink/special member, not just `/` and `..`. Without one, the hardened
            # manual pass is the guard.
            try:
                zf.extractall(self._dir, filter="data")
            except TypeError:
                bad = [n for n in zf.namelist()
                       if n[:1] in ("/", "\\") or re.fullmatch(r"[A-Za-z]:.*", n)
                       or ".." in n.replace("\\", "/").split("/")]
                if bad:
                    raise OSError(f"the archive names paths outside its folder ({bad[:3]})")
                zf.extractall(self._dir)
        (self._marker).write_text(json.dumps({
            "version": RELEASE["version"], "sha256": RELEASE["sha256"],
            "binary": RELEASE["binary"], "at": time.time(),
            "binary_sha256": _sha256_of(self._dir / RELEASE["binary"]),
        }), encoding="utf-8")
        self._remove_part()


def _verify_binary_pe(archive: Path) -> None:
    """The pin promises a 64-bit engine; the archive's binary is checked against the PE
    machine type, not the zip's file name. A 32-bit binary serves, and then fails in the
    search space (address-space ceiling on big books) — the failure the fetch exists to
    prevent, so it is a fetch failure."""
    with zipfile.ZipFile(archive) as zf:
        data = zf.read(RELEASE["binary"])
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise OSError(f"{RELEASE['binary']} in the archive is not a PE executable")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset:offset + 4] != b"PE\x00\x00":
        raise OSError(f"{RELEASE['binary']} in the archive has no PE header")
    machine = struct.unpack_from("<H", data, offset + 4)[0]
    if machine != RELEASE["machine"]:
        raise OSError(f"{RELEASE['binary']} is PE machine 0x{machine:04x} — "
                      f"the pin wants 0x{RELEASE['machine']:04x} (x86_64); the 32-bit build is "
                      "unfit for big books")
