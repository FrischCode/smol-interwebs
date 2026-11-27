"""siw.acquire -- the download queue: one transfer at a time, resumable, hash-verified.

The backend's only long-running job. A transfer is a row id bound to a URL and a part file
(`<name>.zim.part`, the house convention): the queue promotes one at a time, the child writes
the part, the part's byte count is the progress (no output parsing), completion is hash
verification against the checksum the `.meta4` sidecar carries (the catalog's `length` is not a
byte oracle, so the sidecar is the size of record too), and a verified part is renamed to the
`.zim` the directory scan then surfaces.

The worker is a daemon thread inside the process (spawn, sample, verify, promote); `/state`
only reads under a lock, so a poll never waits on a transfer. Every seam that touches the
outside world is an injectable callable -- a network fetch, a child process -- so the test suite
runs the whole state machine against fakes with no socket and no subprocess.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from siw import zimmeta

# One poll of the part file; the console's own poll (2-5 s) is slower, this only bounds how
# stale a `progress` may read.
SAMPLE_INTERVAL = 1.0
# The metalink is a few KB; a cap keeps a hostile sidecar from becoming a memory answer.
META4_CAP = 1024 * 1024
HASH_CHUNK = 1024 * 1024
# How long a sidecar fetch may take before the transfer is failed rather than the queue hung.
META4_TIMEOUT = 30.0

# The queue's row states, as the contract's row machine sees them. `verifying` is internal
# (the part is complete, the hash is running) and maps to `downloading` on the wire.
QUEUED = "queued"
DOWNLOADING = "downloading"
VERIFYING = "verifying"
FAILED = "failed"


class AcquireError(Exception):
    """A transfer refused or failed, with the code a client can branch on and a sentence it can show."""

    def __init__(self, code: str, message: str, retryable: bool = True):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class Transfer:
    """One in-flight or failed download. The on-disk success is not kept here: a renamed `.zim`
    is corpus truth, and the directory scan owns its row."""
    id: str
    url: str
    file: str
    corpus: Path
    state: str = QUEUED
    attempts: int = 0
    size: int | None = None
    sha256: str | None = None
    error: dict | None = None
    child: object | None = None
    part: Path | None = None
    # How a finished transfer verified its bytes: "sha256" (compared against the checksum),
    # "header" (the ZIM header read; no checksum was available), None until promotion.
    verified: str | None = None
    _samples: deque = field(default_factory=lambda: deque(maxlen=8), repr=False)
    _spawned_at: float = 0.0

    @property
    def part_path(self) -> Path:
        return self.corpus / (self.file + ".part")

    @property
    def final_path(self) -> Path:
        return self.corpus / self.file

    def progress(self, queue_position: int) -> dict:
        """The contract's progress object: part bytes over the sidecar's size, rate and eta from
        the recent samples (null while there is not enough of them to be honest)."""
        part = self.part_path
        try:
            have = part.stat().st_size if part.exists() else 0
        except OSError:
            have = 0
        total = self.size or 0
        percent = int(have * 100 // total) if total else 0
        rate = None
        eta = None
        samples = list(self._samples)
        if len(samples) >= 2 and total:
            (t0, b0), (t1, b1) = samples[0], samples[-1]
            span = t1 - t0
            if span > 0 and b1 >= b0:
                rate = int((b1 - b0) / span)
                if rate > 0:
                    eta = int((total - b1) / rate)
        # Cap at 99 while bytes are still arriving; VERIFYING means they all did.
        percent_out = 100 if self.state == VERIFYING else min(percent, 99)
        return {"bytes": have, "total_bytes": total, "percent": percent_out,
                "rate_bytes_s": rate, "eta_seconds": eta, "queue_position": queue_position,
                "url": self.url}

    def error_record(self, partial_kept: bool) -> dict:
        e = self.error or {"code": "unknown", "message": "the transfer ended without a reason"}
        return {"code": e["code"], "message": e["message"], "retryable": e.get("retryable", True),
                "attempts": self.attempts, "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "partial_removed": not partial_kept}



def _spawn_flags() -> dict:
    """Popen keywords per platform: no window on Windows (a curl in a GUI-spawned process is the
    same hang the engine's spawn already had to solve), nothing special elsewhere."""
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def _download_tool() -> tuple[str, list[str]] | None:
    """The shell-out recipe: curl (ships with the OS) first, wget the fallback. `None` names
    the refusal a transfer prints instead of a silent hang."""
    curl = shutil.which("curl")
    if curl:
        return "curl", [curl, "-L", "--fail", "-C", "-", "-o"]
    wget = shutil.which("wget")
    if wget:
        # No -c: wget's own manual says combining -c with -O does not resume correctly.
        # Resume belongs to the curl path; the wget fallback starts fresh.
        return "wget", [wget, "-O"]
    return None


def default_spawn(url: str, part: Path) -> subprocess.Popen:
    """The real child: resume the part where it stopped, follow redirects, fail on HTTP errors."""
    tool, head = _download_tool()
    if tool is None:
        raise AcquireError("no_download_tool",
                           "neither curl nor wget is on PATH; install one or put the .zim in the folder yourself")
    argv = head + [str(part), url]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_spawn_flags())


def parse_meta4(raw: bytes) -> dict:
    """The metalink XML to its facts: the name, the size, the sha-256, the mirrors.

    Pure, so the test suite feeds it real sidecar bytes (namespaced) and bare XML alike. The metalink
    declares a default XML namespace, so the children are matched on their local names, with or
    without it."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AcquireError("meta4_unreadable", f"the sidecar is not the metalink XML ({exc})") from exc
    def _children(el, local_name):
        return [c for c in el if c.tag.rsplit("}", 1)[-1] == local_name]
    file_el = _children(root, "file")[0] if _children(root, "file") else None
    if file_el is None:
        raise AcquireError("meta4_unreadable", "the sidecar carries no <file> element")
    size_els = _children(file_el, "size")
    size_el = size_els[0] if size_els else None
    sha = ""
    for hash_el in _children(file_el, "hash"):
        if hash_el.get("type") == "sha-256":
            sha = (hash_el.text or "").strip().lower()
    return {"name": file_el.get("name"),
            "size": int(size_el.text) if size_el is not None and size_el.text else None,
            "sha256": sha or None,
            "mirrors": [el.get("href") for el in _children(file_el, "url") if el.get("href")]}


def fetch_meta4(url: str) -> dict:
    """The `.meta4` sidecar for a `.zim` URL: the size, the sha-256, the mirrors.

    The sidecar is what kiwix's own browse UI fetches for its download dialog, so reading it is
    using their mechanism, not mirroring their data. A `.meta4` URL passed in is read as-is."""
    if not url.endswith(".meta4"):
        url = url + ".meta4"
    try:
        with urllib.request.urlopen(url, timeout=META4_TIMEOUT) as reply:
            raw = reply.read(META4_CAP + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise AcquireError("meta4_unreachable",
                           f"the sidecar at {url} did not answer ({exc.__class__.__name__})") from exc
    if len(raw) > META4_CAP:
        raise AcquireError("meta4_unreadable", f"the sidecar is {len(raw)} bytes; expected a small metalink")
    facts = parse_meta4(raw)
    if not facts.get("name"):
        facts["name"] = Path(url[:-len(".meta4")]).name
    return facts


def sha256_of(path: Path) -> str:
    """The part's hash, chunked so a 118 GB file is a pass, not a read."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class Acquirer:
    """The queue plus its worker thread. Row ids are the console's row ids, so a transfer is
    always addressable as the row it belongs to."""

    def __init__(self, corpus: str | Path, fetch_meta4: Callable[[str], dict] | None = None,
                 spawn: Callable[[str, Path], subprocess.Popen] | None = None,
                 run_worker: bool = True, poll: float = SAMPLE_INTERVAL):
        # `corpus` may be None on a fresh machine (no config file, no SIW_CORPUS): building
        # the queue still works; `queue()` is where the absence gets a named refusal.
        self.corpus = Path(corpus).expanduser() if corpus else None
        self.verified_by_row: dict[str, str] = {}
        self._promoted_seq = 0  # downloads that reached disk; monotonic, read lock-free
        self._fetch = fetch_meta4 or fetch_meta4_real
        self._spawn = spawn or default_spawn
        self._poll = poll
        self._lock = threading.Lock()
        self._transfers: dict[str, Transfer] = {}
        self._order: list[str] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if run_worker:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="siw-acquire")
            self._thread.start()

    # -- the verbs' entry points ------------------------------------------------

    def queue(self, row_id: str, url: str, file: str, attempts: int = 0,
              sha256: str | None = None) -> dict:
        """Put a row in the queue (or leave it where it is: queuing a queued row is a no-op that
        returns the row's truth, so a double-click cannot start two children). A caller-supplied
        `sha256` rides to the transfer and is what it verifies against -- with one present the
        `.meta4` sidecar is never fetched."""
        if self.corpus is None:
            raise AcquireError("no_corpus",
                               "no corpus directory is set: the download has nowhere to write",
                               retryable=False)
        with self._lock:
            existing = self._transfers.get(row_id)
            if existing and existing.state in (QUEUED, DOWNLOADING, VERIFYING):
                return existing.progress(self._position_locked(row_id))
            if existing:
                existing.error = None
                if sha256:
                    existing.sha256 = sha256
            t = Transfer(id=row_id, url=url, file=file, corpus=self.corpus, attempts=attempts,
                         sha256=sha256)
            self._transfers[row_id] = t
            if row_id not in self._order:
                self._order.append(row_id)
            self._wake.set()
            return t.progress(self._position_locked(row_id))

    def cancel(self, row_id: str) -> dict:
        """Kill the child (if there is one) and take the row out of the queue. The part stays:
        the next download resumes where this one stopped, and that is the whole point of a
        `.part`."""
        with self._lock:
            t = self._transfers.pop(row_id, None)
            if t is None:
                return {"state": "absent", "part_bytes": 0}
            self._order = [i for i in self._order if i != row_id]
        if t.child is not None:
            self._kill(t)
        part_bytes = 0
        if t.part_path.exists():
            try:
                part_bytes = t.part_path.stat().st_size
            except OSError:
                pass
        return {"state": "absent", "part_bytes": part_bytes}

    def fail_if_needed(self, row_id: str, code: str, message: str, retryable: bool = True) -> None:
        """Record a failure the worker hit while the queue itself could not (a sidecar that will
        not answer, a checksum that does not match). The row keeps its part unless the caller
        says otherwise."""
        with self._lock:
            t = self._transfers.get(row_id)
            if t is not None and t.state in (QUEUED, DOWNLOADING, VERIFYING):
                t.state = FAILED
                t.error = {"code": code, "message": message, "retryable": retryable}
                t.child = None
                self._wake.set()

    def promoted_seq(self) -> int:
        """Downloads that reached disk (the rename succeeded), since process start. A reader
        that sees it change knows any scan taken before it is stale — the finished book is
        on disk whether the scan knows it or not."""
        return self._promoted_seq

    def state_of(self, row_id: str) -> str | None:
        """`queued`/`downloading`/`verifying`/`failed` for a transfer the queue knows, `None` for
        a row the queue has no record of (absent, or a finished download whose file is corpus
        truth now)."""
        with self._lock:
            t = self._transfers.get(row_id)
            if t is None:
                return None
            return t.state if t.state != VERIFYING else DOWNLOADING

    def progress(self, row_id: str) -> dict | None:
        with self._lock:
            t = self._transfers.get(row_id)
            if t is None:
                return None
            return t.progress(self._position_locked(row_id))

    def error(self, row_id: str) -> dict | None:
        with self._lock:
            t = self._transfers.get(row_id)
            if t is None or t.error is None:
                return None
            partial_kept = t.part_path.exists() and t.part_path.stat().st_size > 0
            return t.error_record(partial_kept)

    def attempts(self, row_id: str) -> int:
        with self._lock:
            t = self._transfers.get(row_id)
            return t.attempts if t else 0

    def verified_for(self, row_id: str) -> str | None:
        """How a download this queue finished was verified: "sha256" or "header". None
        when this queue did not write the file (it arrived by scan, or before this process)."""
        with self._lock:
            return self.verified_by_row.get(row_id)

    def forget(self, row_id: str) -> None:
        """Drop a finished or cancelled transfer from the record (a `remove` on its row)."""
        with self._lock:
            t = self._transfers.pop(row_id, None)
            self._order = [i for i in self._order if i != row_id]
        if t is not None and t.child is not None:
            self._kill(t)

    def set_corpus(self, corpus: str | Path | None) -> None:
        """Re-target the queue when the operator points at a different folder. Refuses
        while a transfer is active: a part belongs to the folder it started in."""
        with self._lock:
            if self._active_locked() is not None:
                raise AcquireError("transfer_active",
                                   "a download is in flight; it finishes into the folder it "
                                   "started in -- cancel it before moving the corpus")
            self.corpus = Path(corpus).expanduser() if corpus else None

    def active_id(self) -> str | None:
        with self._lock:
            return self._active_locked()

    def busy(self) -> bool:
        with self._lock:
            return self._active_locked() is not None

    # -- the worker ------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:  # a worker that dies is a queue that silently stops
                pass
            self._wake.clear()
            self._wake.wait(self._poll)

    def stop(self, join_timeout: float = 2.0) -> None:
        """Settle the queue: stop the worker, and kill the child still writing a part.
        `siw serve` leaving a curl running past its own exit is the bug this closes."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            active = self._active_locked()
            t = self._transfers.get(active) if active else None
        if t is not None and t.child is not None:
            self._kill(t)
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def step(self) -> None:
        """One pass: reap the active transfer, then promote the queue's head into it.

        Public (not just loop-called) because the test suite drives the state machine with fake
        children that finish instantly: a test calls `step()` instead of sleeping for the
        worker's tick, and gets the same transitions in the same order the live queue gives."""
        self._reap()
        if not self._busy_now():
            self._promote()

    def _busy_now(self) -> bool:
        with self._lock:
            return self._active_locked() is not None

    def _active_locked(self) -> str | None:
        for row_id in list(self._order):
            t = self._transfers.get(row_id)
            if t is not None and t.state in (DOWNLOADING, VERIFYING):
                return row_id
        return None

    def _position_locked(self, row_id: str) -> int:
        if row_id not in self._order:
            return 0
        pos = self._order.index(row_id)
        active = self._active_locked()
        if active is None:
            return 0
        if active == row_id:
            return 0
        return 1 + sum(1 for i in self._order[:pos]
                       if (s := self._transfers[i].state) in (QUEUED, DOWNLOADING, VERIFYING))

    def _reap(self) -> None:
        with self._lock:
            active = self._active_locked()
            t = self._transfers.get(active) if active else None
        if t is None or t.child is None:
            return
        # A sample, whether or not the child is still writing.
        t._samples.append((time.monotonic(), _part_bytes(t.part_path)))
        if t.state == DOWNLOADING:
            rc = t.child.poll()
            if rc is None:
                return  # still writing; the next sample picks it up
            if rc != 0:
                # A child that died mid-transfer has not finished: the part stays for a
                # resume, and the failure names the exit code instead of blaming the hash.
                self._fail(t, "transfer_failed",
                           f"the download ended with exit code {rc}; the part is kept for a retry")
                return
            t.state = VERIFYING
        if t.state == VERIFYING:
            self._verify(t)

    def _verify(self, t: Transfer) -> None:
        """Two doors to promotion, never a third: with a checksum, compare it; without one,
        the part must read as a ZIM (the 80-byte header). The transfer ending is not itself
        a verification of anything."""
        try:
            got = sha256_of(t.part_path)
        except OSError as exc:
            self._fail(t, "verify_failed", f"the part could not be read for its hash ({exc})")
            return
        want = t.sha256
        if want:
            if got != want:
                try:
                    t.part_path.unlink()
                except OSError:
                    pass
                self._fail(t, "checksum_mismatch",
                           f"checksum mismatch — the file hashes to {got[:12]}…, "
                           f"the checksum expects {want[:12]}…")
                return
            t.verified = "sha256"
        elif zimmeta.read_header(t.part_path):
            t.verified = "header"
        else:
            self._fail(t, "header_unreadable",
                       "no checksum was available and the part does not read as a ZIM "
                       "(its first 80 bytes are no header); the part is kept for a retry")
            return
        try:
            t.part_path.replace(t.final_path)
        except OSError as exc:
            self._fail(t, "rename_failed", f"the verified part could not become the .zim ({exc})")
            return
        with self._lock:
            self._transfers.pop(t.id, None)
            self._order = [i for i in self._order if i != t.id]
            self.verified_by_row[t.id] = t.verified or "header"
            self._promoted_seq += 1
            self._wake.set()

    def _fail(self, t: Transfer, code: str, message: str, retryable: bool = True) -> None:
        with self._lock:
            t.state = FAILED
            t.error = {"code": code, "message": message, "retryable": retryable}
            t.child = None
            self._wake.set()

    def _promote(self) -> None:
        with self._lock:
            row_id = None
            for candidate in self._order:
                t = self._transfers.get(candidate)
                if t is not None and t.state == QUEUED:
                    row_id = candidate
                    break
            if row_id is None:
                return
            t = self._transfers[row_id]
        # Sidecar first: the size and the checksum are what the transfer verifies against, and
        # a sidecar that will not answer fails the row here rather than after a gigabyte of
        # bytes with no oracle. A caller who brought the checksum skips the fetch entirely.
        if not t.sha256:
            try:
                meta = self._fetch(t.url)
            except AcquireError as exc:
                with self._lock:
                    t.state = FAILED
                    t.error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
                return
            t.size = meta.get("size")
            t.sha256 = meta.get("sha256")
        try:
            t.child = self._spawn(t.url, t.part_path)
        except AcquireError as exc:
            with self._lock:
                t.state = FAILED
                t.error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
            return
        except OSError as exc:
            self._fail(t, "spawn_failed", f"the download child could not start ({exc})")
            return
        with self._lock:
            t.state = DOWNLOADING
            t._spawned_at = time.monotonic()
            t._samples.clear()
            t._samples.append((time.monotonic(), _part_bytes(t.part_path)))

    def _kill(self, t: Transfer) -> None:
        try:
            t.child.terminate()  # type: ignore[union-attr]
            t.child.wait(timeout=5)  # type: ignore[union-attr]
        except (OSError, subprocess.TimeoutExpired, AttributeError):
            pass
        t.child = None


def _part_bytes(part: Path) -> int:
    try:
        return part.stat().st_size
    except OSError:
        return 0


def fetch_meta4_real(url: str) -> dict:
    """The seam name the constructor takes: `fetch_meta4` by default, a fake in the test suite."""
    return fetch_meta4(url)

