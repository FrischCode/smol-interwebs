"""siw.console -- the web console backend: `/state`, `/action`, and the page itself.

One origin, two routes, one page: the console is served by the SIW server (no second port),
`GET /state` answers the whole snapshot (engine down included, internet never touched), and
`POST /action` takes a verb and its args and returns the freshest truth it has. The page is a
single static file; refresh is a poll.

Everything that touches the outside world is a seam -- an engine probe, a catalog read, a
directory scan, a download spawn -- so the test suite builds a console over fakes and asserts the
wire shape without a socket, and the live server is the same class with the real functions in.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from siw import __version__
from siw import acquire, api, config as config_module, corpus, engine, tools, zimmeta

# The snapshot's freshness bounds, as the contract's "be quick" rule sets them: the engine probe
# is bounded so a dead port cannot hold a poll, and the scan is cached so a poll does not walk
# the corpus volume on every tick.
PROBE_TIMEOUT = 0.3
PROBE_TTL = 2.0
BODY_CAP = 4 * 1024 * 1024     # a POST body larger than this is refused, not buffered (m15)
PREVIEW_KEEP = 64              # pasted-URL previews remembered for the confirm gate (m8)
TASK_KEEP = 20                 # finished move tasks kept for the page's sake (m8)
SCAN_TTL = 5.0
CATALOG_TTL = 10.0
POLL_WORK_MS = 2000
POLL_IDLE_MS = 5000

# The verbs and the args each takes. A key outside this table is a 400 that names what it
# accepted; the table is the contract's verb list, so an unknown verb's 404 can name the rest.
VERBS: dict[str, tuple[str, ...]] = {
    "engine_start": (),
    "engine_stop": (),
    "engine_restart": (),
    "config_apply": (),
    "config_discard": (),
    "download": ("id",),
    "download_all": (),
    "cancel": ("id",),
    "retry": ("id",),
    "remove": ("id",),
    "scan": (),
    "preview": ("url",),
    "download_url": ("url", "sha256"),
    "set_settings": ("bind", "port", "username", "password"),
    "set_corpus_dir": ("path", "move"),
    "task_cancel": ("id",),
    "open_folder": (),
    "dismiss": ("id",),
}

# The optional args: present means a value, absent means unchanged. The verb's tuple minus
# these is the required set.
OPTIONAL_ARGS: dict[str, set[str]] = {
    "download_url": {"sha256"},
    "set_settings": {"bind", "port", "username", "password"},
    "set_corpus_dir": {"move"},
}

# Settings fields and what applying them needs: a server field restarts this process (the bound
# socket is the field), a corpus/engine field restarts the engine it serves.
SETTING_FIELDS = {
    "bind": ("Server address", "server_restart"),
    "port": ("Server port", "server_restart"),
    "username": ("Console username", "server_restart"),
    "password": ("Console password", "server_restart"),
}

# The config field each console setting writes. The page's "Server port" is the port this
# server answers on -- `api_port`; the engine's own port is not a console setting.
SETTING_CONFIG_FIELD = {"bind": "bind", "port": "api_port", "username": "username",
                        "password": "password"}



class ConsoleError(Exception):
    """A verb refusal: the HTTP status to answer with and the error object to put in the body."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def load_offer(path: str | Path | None = None) -> dict:
    """The offered set (`profiles/offer.json`): the rows the list always carries, in list order.

    Missing or unreadable offers load as an empty set -- the console is a management surface for
    whatever is on disk, and a broken data file must not take the whole page down with it; the
    page still shows the scanned rows and the last tier."""
    where = Path(path) if path else default_offer_path()
    if not where.is_file():
        return {"catalog_link": "https://browse.library.kiwix.org", "books": []}
    try:
        raw = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"catalog_link": "https://browse.library.kiwix.org", "books": []}
    return {"catalog_link": raw.get("catalog_link") or "https://browse.library.kiwix.org",
            "books": raw.get("books") or []}


def default_offer_path() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent / "profiles" / "offer.json"


def default_profiles_path() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent / "profiles" / "books.jsonl"


def load_profiles(path: str | Path | None = None) -> dict:
    """The optional title lookup (`profiles/books.jsonl` when a user has dropped one in),
    indexed the two ways identity uses it: by the book's own UUID and by its file stem.
    The shipped tree carries no such file, so the usual index is empty and identity
    resolves from the file itself (the filename rung, then the embedded header)."""
    where = Path(path) if path else default_profiles_path()
    by_uuid: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    if not where.is_file():
        return {"by_uuid": by_uuid, "by_name": by_name}
    for line in where.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        media = "text+images" if int(entry.get("mediaCount") or 0) > 0 else "text"
        record = {"name": entry.get("name") or "", "title": entry.get("title") or "",
                  "media": media, "uuid": entry.get("uuid") or ""}
        if entry.get("uuid"):
            by_uuid[entry["uuid"].lower()] = record
        if entry.get("serverName"):
            by_name[entry["serverName"].lower()] = record
    return {"by_uuid": by_uuid, "by_name": by_name}


def _iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))


# One loopback predicate for the whole tool, living beside the kernel it guards; the
# console and its tests keep calling it through this name.
is_loopback = api.is_loopback


def _basic_auth_ok(header: str, username: str, password: str) -> bool:
    """The Basic check, separated from the HTTP layer so the test suite tests it as a function.

    The comparison is constant-time on the credentials: a console that answers in 2 ms for a
    wrong password and 40 ms for the right one is a timing oracle."""
    import base64
    import hmac
    prefix = "Basic "
    if not header.startswith(prefix):
        return False
    try:
        decoded = base64.b64decode(header[len(prefix):].strip()).decode("utf-8", "replace")
    except Exception:
        return False
    user, sep, pw = decoded.partition(":")
    if not sep:
        return False
    return (hmac.compare_digest(user.encode(), username.encode())
            and hmac.compare_digest(pw.encode(), password.encode()))


@dataclass
class MoveTask:
    """The one operation long enough to deserve the `tasks` lane: moving the corpus folder."""
    id: str
    source: str
    target: str
    state: str = "running"
    bytes: int = 0
    done_bytes: int = 0
    started: float = 0.0
    error: str | None = None
    # Annotated and factory-built: without the annotation this was a CLASS attribute --
    # one Event shared by every MoveTask, so cancelling one move cancelled all of them
    # and any later move started already-cancelled.
    _cancel: threading.Event = field(default_factory=threading.Event)

    def record(self) -> dict:
        total = self.bytes or 1
        rate = None
        eta = None
        span = time.time() - self.started
        if span > 0 and self.state == "running":
            rate = int(self.done_bytes / span)
            if rate > 0:
                eta = int((total - self.done_bytes) / rate)
        return {"id": self.id, "kind": "move_corpus",
                "label": f"moving {Path(self.source).name} to {self.target}",
                "state": self.state, "bytes": self.bytes, "done_bytes": self.done_bytes,
                "rate_bytes_s": rate, "eta_seconds": eta, "from": self.source, "to": self.target,
                "error": self.error}


class Console:
    """The console's whole state: the config it runs on, the queue it drives, the caches that
    keep a poll cheap, and the verbs' dispatch. Built once per server process."""

    def __init__(self, config, webdir: str | Path | None = None,
                 offer: dict | None = None, profiles: dict | None = None,
                 acquirer: acquire.Acquirer | None = None,
                 probe=None, catalog=None, listening=None, version=None,
                 launch=None, stop_engine=None, clock=None, reexec=None, fetcher=None):
        self.config = config
        self.webdir = Path(webdir) if webdir else default_webdir()
        self.offer = offer or load_offer()
        self.profiles = profiles or load_profiles()
        self._probe = probe or _real_probe(config)
        self._catalog = catalog or (lambda: engine.catalog(config, count=2000))
        self._listening = listening or (lambda port: engine.listening_pids(port))
        self._version = version or (lambda exe: engine.version(exe))
        self._launch = launch or (lambda books: engine.launch(config, books))
        self._stop_engine = stop_engine or (lambda: engine.stop(config))
        self._clock = clock or time.time
        self.acquirer = acquirer or acquire.Acquirer(config.corpus, run_worker=True)
        self._started_at = self._clock()
        self._probe_cache: tuple[float, bool] | None = None
        self._bound_cache: tuple[float, bool] | None = None
        self._known_rows: set[str] | None = None
        self._catalog_cache: tuple[float, list[dict], str] | None = None
        self._scan_cache: tuple[float, list, list, list] | None = None
        self._seen_promoted_seq = 0
        self._notice_state: dict[str, dict] = {}
        self._dismissed: set[str] = set()
        self._pending: list[dict] = []
        self._tasks: dict[str, MoveTask] = {}
        self._fetcher = (fetcher if fetcher is not None
                         else tools.ToolsFetcher(tools.default_tools_dir()))
        self._fetcher.set_on_ready(self._on_binary_ready)
        self._engine_wanted = False
        self._spawn_error: str | None = None
        self._seen: dict[str, str] = {}          # row id -> last confirmed at
        self._prev_states: dict[str, str] = {}   # row id -> last polled state
        self._last_row_ids: set[str] | None = None  # what the page last saw: `added` is against it
        self._secrets: dict[str, object] = {}    # password values that must not ride the wire
        self._previews: dict[str, dict] = {}     # resolved zim url -> preview
        self._extra_rows: list[dict] = []        # paste-added rows not yet on disk
        self._extra_path: Path | None = _extra_rows_path(config)
        if self._extra_path is not None:
            self._load_extra()
        self._runtime_corpus: str | None = config.corpus
        self._bound: tuple[str, int] | None = None
        self._engine_starting_until = 0.0
        self._engine_stopping_until = 0.0
        self._reexec_fn = reexec or self._reexec
        self._lock = threading.Lock()
        # Engine lifecycle acts serialise here: the boot thread and an engine_start verb
        # probing the same dark port would otherwise launch two servers, the loser of the
        # port race idling orphaned -- the failure named in siw/engine.py's header.
        self._engine_lock = threading.RLock()

    # -- the snapshot -----------------------------------------------------------

    def set_bound(self, host: str, port: int) -> None:
        """The socket's own word for what it answers on, reported in `server`."""
        self._bound = (host, port)

    def _bound_now(self, port: int) -> bool:
        """Does anything listen on this port? Cached like the probe: `netstat -ano` measured
        ~35 ms, and the page polls every couple of seconds."""
        now = self._clock()
        if self._bound_cache and now - self._bound_cache[0] < PROBE_TTL:
            return self._bound_cache[1]
        bound = bool(self._listening(port))
        self._bound_cache = (now, bound)
        return bound

    def _corpus_dir(self) -> str | None:
        return self._runtime_corpus

    def scan(self, force: bool = False) -> tuple[list, list, list]:
        """The corpus directory as books, parts, and errors -- cached for the poll's sake,
        refreshed on force (the `scan` verb) or age."""
        now = self._clock()
        if not force and self._scan_cache and now - self._scan_cache[0] < SCAN_TTL:
            return self._scan_cache[1], self._scan_cache[2], self._scan_cache[3]
        where = self._corpus_dir()
        if not where or not Path(where).is_dir():
            self._scan_cache = (now, [], [], [] if not where else [f"corpus directory {where} is not a directory"])
            return self._scan_cache[1], self._scan_cache[2], self._scan_cache[3]
        books = corpus.list_books(where)
        parts = corpus.parts(where)
        errors: list[str] = []
        # Parts are facts the corpus block carries, not scan errors: they come back as
        # themselves and never fold into `errors`.
        self._scan_cache = (now, books, parts, errors)
        return books, parts, errors

    def _bin(self, version: str | None) -> dict:
        """The binary's truth for the wire: a path the user named, a pinned binary SIW
        installed itself, or the fetch's state. One key set either way."""
        if self.config.exe:
            # A user-named exe stays `configured`; the one the fetch installed is `fetched`
            # — the config's source map is the record of who set it, and the runtime section
            # is exactly where that distinction is useful.
            state = "fetched" if self.config.sources.get("exe") == "siw-tools" else "configured"
            exe = self.config.exe
            return {"state": state, "version": version, "path": exe,
                    "exists": Path(exe).is_file(), "progress": None, "error": None}
        return dict(self._fetcher.snapshot())

    def _bring_up(self, raise_errors: bool) -> dict:
        """One lifecycle act at a time (a start is seconds, not minutes), then the body."""
        with self._engine_lock:
            return self._bring_up_locked(raise_errors)

    def _bring_up_locked(self, raise_errors: bool) -> dict:
        """The shared body of "start the engine": ensure the binary (fetching it when the
        config names none), then spawn. The verb raises its errors; the boot thread and
        the fetch-completion callback do not — they park the failure where the next poll
        can read it."""
        cfg = self.config
        self._engine_wanted = True
        if not cfg.exe:
            snap = self._fetcher.snapshot()
            if snap["state"] == "ok":
                cfg.set("exe", snap["path"], source="siw-tools")
            elif snap["state"] in ("fetching", "verifying", "extracting"):
                if raise_errors:
                    raise ConsoleError(409, "binary_fetching",
                                       "the kiwix-serve download is in progress — "
                                       "the engine starts when it lands")
                return {"engine": self.engine_snapshot()}
            else:  # missing or failed: the fetch is the act; it will start the engine itself
                self._fetcher.start()
                return {"engine": self.engine_snapshot()}
        books = self.scan()[0]
        if not books:
            if raise_errors:
                raise ConsoleError(409, "corpus_empty", "the corpus folder holds no .zim files to serve")
            return {"engine": self.engine_snapshot()}
        if self._probe(cfg.host, int(cfg.port), PROBE_TIMEOUT):
            return {"engine": self.engine_snapshot()}
        # The window must outlast the launch itself: engine.launch polls for up to
        # STARTUP_BUDGET, and a shorter window re-enabled Start mid-launch.
        self._engine_starting_until = self._clock() + engine.STARTUP_BUDGET
        try:
            self._launch(books)
        except (engine.EngineError, OSError) as exc:
            self._engine_starting_until = 0.0
            if raise_errors:
                raise ConsoleError(502, "spawn_failed",
                                   f"the engine could not be started ({exc}); see {engine.log_path(cfg)}") from exc
            self._spawn_error = f"the engine could not be started ({exc})"
            return {"engine": self.engine_snapshot()}
        self._spawn_error = None
        return {"engine": self.engine_snapshot()}

    def _on_binary_ready(self) -> None:
        """The fetch worker's hand-off: a wanted engine, a landed binary — spawn it.
        Runs on the worker thread, never under the fetcher's lock."""
        if not self._engine_wanted:
            return
        try:
            self._bring_up(raise_errors=False)
        except Exception:
            pass  # the next /state shows the parked error; a callback must not raise

    def engine_snapshot(self) -> dict:
        """The engine's truth for this poll: a bounded probe, a fresh catalog only when up,
        and the pidfile's word on whether the process is ours to stop."""
        cfg = self.config
        now = self._clock()
        if self._probe_cache and now - self._probe_cache[0] < PROBE_TTL:
            up = self._probe_cache[1]
        else:
            up = bool(self._probe(cfg.host, int(cfg.port), PROBE_TIMEOUT))
            self._probe_cache = (now, up)
        entries: list[dict] = []
        problem = ""
        if up:
            if self._catalog_cache and now - self._catalog_cache[0] < CATALOG_TTL:
                entries, problem = self._catalog_cache[1], self._catalog_cache[2]
            else:
                try:
                    entries, problem = self._catalog()
                except Exception as exc:
                    entries, problem = [], f"the engine's catalog could not be read ({exc.__class__.__name__})"
                self._catalog_cache = (now, entries, problem)
        record, _where = engine.read_pidfile(cfg)
        version = None
        if cfg.exe:
            try:
                version, _where2 = self._version(cfg.exe)
            except Exception:
                version = None
        # Confirmations: which on-disk books the running engine reports right now.
        books, _parts, _errs = self.scan()
        served_names = {e["content_name"].lower() for e in entries if e.get("content_name")}
        confirmed_any = False
        with self._lock:
            for book in books:
                if book.name.lower() in served_names:
                    confirmed_any = True
                    self._seen[book.name] = _iso(now)
        if up:
            state = "up"
        elif now < self._engine_stopping_until:
            state = "stopping"
        elif now < self._engine_starting_until:
            state = "starting"
        else:
            state = "down"
        if not up and not problem:
            bin = self._bin(version)
            if bin["state"] in ("fetching", "verifying", "extracting"):
                p = bin["progress"] or {}
                total = p.get("total") or 0
                got = p.get("received") or 0
                pct = round(100 * got / total) if total else 0
                problem = (f"fetching kiwix-serve {bin['version']} — {pct}% "
                           f"({_human(got)} of {_human(total)})")
            elif bin["state"] == "failed":
                problem = f"the kiwix-serve download failed ({bin['error']}) — Start retries"
            elif bin["state"] == "missing" and not cfg.exe:
                problem = None  # a bare DOWN is true: nothing is listening. Fetching starts on demand.
            elif bin["state"] == "configured" and not bin["exists"]:
                problem = f"the configured binary {cfg.exe} is not on disk"
            elif self._spawn_error:
                problem = self._spawn_error
            elif record and not engine.pid_alive(int(record.get("pid") or 0)):
                problem = (f"nothing listening on {cfg.host}:{cfg.port} "
                           f"(last SIW pid {record.get('pid')} is gone)")
            else:
                problem = None  # nothing to add to "it is down" — the port says so itself
        return {"state": state, "up": bool(up), "host": cfg.host, "port": int(cfg.port),
                "url": f"http://{cfg.host}:{cfg.port}", "exe": cfg.exe, "version": version,
                "bin": self._bin(version),
                "form": (record or {}).get("form"), "bound_on": self._bound_now(int(cfg.port)),
                "started": _iso(float(record["started"])) if record and record.get("started") else None,
                "pid": int(record["pid"]) if record and engine.pid_alive(int(record.get("pid") or 0)) else None,
                "started_by_siw": bool(record),
                "served_books": len(entries) if up else None,
                "configured_books": len(books),
                "drift": corpus.diff([b.name for b in books], [e["content_name"] for e in entries]) if up else None,
                "problem": problem or None,
                "log_path": str(engine.log_path(cfg)) if cfg.exe else None,
                "uptime_seconds": (now - float(record["started"])) if record and engine.pid_alive(int(record.get("pid") or 0)) else None,
                "last_checked": _iso(now),
                "_confirmed": confirmed_any}

    def server_snapshot(self) -> dict:
        cfg = self.config
        bound_host, bound_port = self._bound or (cfg.bind, int(cfg.api_port))
        loopback = is_loopback(bound_host)
        credentials = {
            "mode": "basic" if cfg.password else "none",
            "configured": bool(cfg.password),
            "username": cfg.username or None,
        }
        warning = None
        if not loopback and not cfg.password:
            warning = (f"bound {bound_host}:{bound_port} without credentials — a reachable console "
                       "is a share; set a password in Settings (a non-loopback bind refuses to save "
                       "without one)")
        return {"bind": cfg.bind, "port": int(cfg.api_port),
                "url": f"http://{bound_host}:{bound_port}", "loopback": loopback,
                "bound_on": [bound_host], "credentials": credentials, "warning": warning,
                "uptime_seconds": round(self._clock() - self._started_at, 1),
                "config_path": str(cfg.path) if cfg.path else None,
                "platform": config_module.platform_note()}

    def corpus_snapshot(self) -> dict:
        where = self._corpus_dir()
        exists = bool(where) and Path(where).is_dir()
        books, parts, errors = self.scan()
        free = total = None
        if exists:
            try:
                usage = shutil.disk_usage(where)
                free, total = usage.free, usage.total
            except OSError:
                pass
        return {"directory": where, "exists": exists, "bytes_free": free, "bytes_total": total,
                "books": len(books), "bytes": sum(b.size for b in books),
                "parts": [{"name": name, "bytes": _safe_size(Path(where) / name)} for name in parts],
                "scanned_at": _iso(self._scan_cache[0]) if self._scan_cache else None,
                "scan_errors": errors}

    def _rows(self, engine: dict) -> list[dict]:
        seq = self.acquirer.promoted_seq()
        if seq != self._seen_promoted_seq:
            self._seen_promoted_seq = seq
            self._scan_cache = None  # a book just landed; a scan taken before it is a lie
        books, _parts, _errs = self.scan()
        disk = {b.name.lower(): b for b in books}
        confirmed_names = set()
        media_counts: dict[str, int | None] = {}
        if engine["up"]:
            if self._catalog_cache:
                for e in self._catalog_cache[1]:
                    if e.get("content_name"):
                        confirmed_names.add(e["content_name"].lower())
                        media_counts[e["content_name"].lower()] = e.get("media_count")
        rows: list[dict] = []
        claimed: set[str] = set()
        for offer_row in self.offer["books"]:
            file = offer_row["file"]
            book = disk.get(Path(file).stem.lower())
            transfer_state = self.acquirer.state_of(offer_row["id"])
            if book is not None:
                claimed.add(book.name.lower())
                rows.append(self._row_on_disk(offer_row, book, engine, confirmed_names, media_counts))
            elif transfer_state is not None:
                rows.append(self._row_in_flight(offer_row, transfer_state))
            else:
                rows.append(self._row_absent(offer_row))
        # Scanned books the offered set does not claim: they are rows too.
        for book in books:
            if book.name.lower() in claimed:
                continue
            if not Path(book.path).exists():
                continue  # gone since the scan: the next scan will settle it
            rows.append(self._row_scanned(book, engine, confirmed_names, media_counts))
        # Paste-added rows that are not on disk yet and not in the queue.
        for extra in list(self._extra_rows):  # snapshot: writers rebind the list under the lock
            if extra["id"] in {r["id"] for r in rows}:
                continue
            transfer_state = self.acquirer.state_of(extra["id"])
            if transfer_state is None:
                rows.append(self._row_absent(extra))
            else:
                rows.append(self._row_in_flight(extra, transfer_state))
        return rows

    def _row_absent(self, spec: dict) -> dict:
        return {"id": spec["id"], "book": spec["book"], "title": spec["title"],
                "build": spec.get("build"), "builds": spec.get("builds"),
                "media": spec.get("media") or "unknown",
                "size_bytes": spec["size_bytes"], "state": "absent",
                "note": spec.get("note"), "defaulted": spec.get("defaulted", True),
                "ident": None, "file": None, "served": None, "progress": None, "error": None,
                "source_url": spec.get("url"), "actions": ["download"]}

    def _row_in_flight(self, spec: dict, transfer_state: str) -> dict:
        state = "downloading" if transfer_state in ("downloading", "verifying") else transfer_state
        row = self._row_absent(spec)
        row.update({"state": state,
                    "note": (f"writing {Path(spec.get('url') or spec['id']).name}.part"
                             if state == "downloading"
                             else (spec.get("note") or "waiting in the queue")),
                    "progress": self.acquirer.progress(spec["id"]),
                    "error": self.acquirer.error(spec["id"]) if state == "failed" else None,
                    "actions": ["cancel"] if state in ("queued", "downloading") else ["retry", "remove"]})
        return row

    def _ident_for(self, stem: str, path: Path, title: str | None) -> dict:
        """The identity ladder as a record: the stem first (high confidence, it is the file's
        own name), then the file's embedded UUID where the stem is not known (the record's
        uuid is the book's own id; the title stays the profile's word or null)."""
        date = ""
        import re
        match = re.search(r"(\d{4}-\d{2})(-\d{2})?$", stem)
        if match:
            date = match.group(1)
        profile = self.profiles["by_name"].get(stem.lower())
        if profile or title:
            return {"rung": "filename", "name": stem,
                    "title": title or (profile or {}).get("title") or None,
                    "date": date or None, "confidence": "high"}
        uuid = zimmeta.book_uuid(path)
        by_uuid = self.profiles["by_uuid"].get(uuid.lower()) if uuid else None
        if uuid:
            return {"rung": "metadata", "name": stem, "title": (by_uuid or {}).get("title") or None,
                    "date": date or None, "confidence": "high" if by_uuid else "low"}
        return {"rung": "filename", "name": stem, "title": title, "date": date or None,
                "confidence": "low"}

    @staticmethod
    def _media_from_counts(media_counts: dict[str, int | None] | None, name: str) -> str | None:
        """A book's content per the running engine's catalog: `text+images` where the book
        reports media, `text` where it reports none, `None` where the engine (or its
        catalog) says nothing at all."""
        if not media_counts:
            return None
        count = media_counts.get(name.lower())
        if count is None:
            return None
        return "text+images" if count > 0 else "text"

    def _row_on_disk(self, spec: dict, book: "corpus.Book", engine: dict, confirmed_names: set[str],
                     media_counts: dict[str, int | None] | None = None) -> dict:
        confirmed = book.name.lower() in confirmed_names
        if confirmed:
            self._seen[book.name] = engine["last_checked"]
        state = "served" if (confirmed or book.name in self._seen) else "downloaded"
        title = spec.get("title")
        profile = self.profiles["by_name"].get(book.name.lower())
        if profile and not title:
            title = profile.get("title")
        # The running engine's catalog is the newest word on a served book's content; the
        # offer's stated value and the profile's are priors, "unknown" is the floor.
        media = self._media_from_counts(media_counts, book.name) or spec.get("media") \
            or (profile or {}).get("media") or "unknown"
        url = f"{engine['url']}/content/{book.name}/" if engine["up"] else None
        seen = self._seen.get(book.name)
        return {"id": spec["id"], "book": spec["book"], "title": title or spec["title"],
                "build": spec.get("build"), "builds": spec.get("builds"), "media": media,
                "size_bytes": book.size, "state": state, "note": None,
                "defaulted": spec.get("defaulted", True),
                "ident": self._ident_for(book.name, Path(book.path), title),
                "file": {"name": Path(book.path).name, "path": book.path, "bytes": book.size,
                         "mtime": _iso(Path(book.path).stat().st_mtime), "sha256": None,
                         "verified": self.acquirer.verified_for(spec["id"])},
                "served": {"configured": True, "confirmed": confirmed, "seen_at": seen,
                           "url": url, "page_url": url},
                "progress": None, "error": None, "source_url": spec.get("url"),
                "actions": ["remove", "scan"]}

    def _row_scanned(self, book: "corpus.Book", engine: dict, confirmed_names: set[str],
                     media_counts: dict[str, int | None] | None = None) -> dict:
        profile = self.profiles["by_name"].get(book.name.lower())
        uuid = zimmeta.book_uuid(Path(book.path))
        by_uuid = self.profiles["by_uuid"].get(uuid.lower()) if uuid else None
        source = profile or by_uuid or {}
        title = source.get("title") or None
        confirmed = book.name.lower() in confirmed_names
        state = "served" if (confirmed or book.name in self._seen) else "downloaded"
        size = book.size
        # The link is the engine's, not the offer's: a scanned-in book opens exactly as an
        # offered one does, once the engine is up.
        url = f"{engine['url']}/content/{book.name}/" if engine["up"] else None
        return {"id": book.name, "book": book.short_name, "title": title or book.short_name,
                "build": None, "builds": None,
                "media": self._media_from_counts(media_counts, book.name) or source.get("media") or "unknown",
                "size_bytes": size, "state": state,
                "note": (f"in the folder since {self._seen.get(book.name, 'now')}" if self._seen.get(book.name) else None),
                "defaulted": True,
                "ident": self._ident_for(book.name, Path(book.path), title),
                "file": {"name": Path(book.path).name, "path": book.path, "bytes": size,
                         "mtime": _iso(Path(book.path).stat().st_mtime), "sha256": None,
                         "verified": self.acquirer.verified_for(book.name)},
                "served": {"configured": True, "confirmed": confirmed,
                           "seen_at": self._seen.get(book.name),
                           "url": url, "page_url": url},
                "progress": None, "error": None, "source_url": None,
                "actions": ["remove", "scan"]}

    def _notices(self, engine: dict, corpus_snap: dict, rows: list[dict]) -> list[dict]:
        built: dict[str, dict] = {}
        # A row that just finished downloading earns a done-notice once (dismissable).
        for row in rows:
            if row["state"] in ("downloaded", "served"):
                before = self._prev_states.get(row["id"])
                if before in ("queued", "downloading", "failed"):
                    # The sentence follows the verification the queue actually performed:
                    # hash-compared earns "and verified", a header check or none does not.
                    how = self.acquirer.verified_for(row["id"])
                    done = "Downloaded and verified" if how == "sha256" else "Downloaded"
                    built[f"download-done:{row['id']}"] = {
                        "id": f"download-done:{row['id']}", "level": "ok",
                        "text": f"{done} {row['file']['name']} ({_human(row['size_bytes'])}).",
                        "action": None}
        self._prev_states = {row["id"]: row["state"] for row in rows}
        if engine["state"] == "down" and engine["configured_books"] > 0:
            n = engine["configured_books"]
            built["engine-down"] = {
                "id": "engine-down", "level": "warn",
                "text": (f"Engine is down — {n} {'book' if n == 1 else 'books'} on disk, none served. "
                         "Search and page links are dead until it is started."),
                "action": {"verb": "engine_start", "label": "Start engine"}}
        if engine["state"] == "down":
            bin = engine["bin"]
            if bin["state"] == "failed":
                built["binary_failed"] = {
                    "id": "binary_failed", "level": "warn",
                    "text": (f"The pinned kiwix-serve download failed: {bin['error']}. "
                             "Start retries the download."),
                    "action": {"verb": "engine_start", "label": "Retry download"}}
            elif bin["state"] == "configured" and not bin["exists"]:
                built["binary_missing"] = {
                    "id": "binary_missing", "level": "warn",
                    "text": (f"The configured kiwix-serve ({bin['path']}) is not on disk — point exe "
                             "at a real binary, or clear it and let SIW fetch the pinned one."),
                    "action": None}
        if (engine["state"] == "up" and engine["served_books"] is not None
                and engine["served_books"] > (engine["configured_books"] or 0)):
            built["foreign-corpus"] = {
                "id": "foreign-corpus", "level": "warn",
                "text": (f"The engine serves {engine['served_books']} books but the corpus directory "
                         f"only accounts for {engine['configured_books']} — it was started elsewhere, "
                         "against a corpus this config does not name. Point the config at that corpus "
                         "(the corpus field in settings), or stop it and let SIW start the engine."),
                "action": None}
        if not corpus_snap["exists"] and corpus_snap["directory"]:
            built["corpus-missing"] = {
                "id": "corpus-missing", "level": "bad",
                "text": f"The corpus directory {corpus_snap['directory']} is not a directory — "
                        "every on-disk row is wrong until it is.",
                "action": None}
        free = corpus_snap["bytes_free"] or 0
        pending = sum(r["size_bytes"] for r in rows
                      if r["state"] == "absent" and r.get("defaulted", True))
        if pending and pending > free:
            built["over-capacity"] = {
                "id": "over-capacity", "level": "warn",
                "text": (f"The default set needs {_human(pending)} but only {_human(free)} is free in "
                         "the corpus folder — free the folder or move it before downloading."),
                "action": None}
        out = []
        with self._lock:
            for notice in built.values():
                if notice["id"] in self._dismissed:
                    continue
                old = self._notice_state.get(notice["id"])
                notice = dict(notice)
                notice["since"] = old["since"] if old else _iso()
                self._notice_state[notice["id"]] = notice
                out.append(notice)
            # A dismissed notice whose condition is gone stops being dismissed.
            for gone in [i for i in self._dismissed if i not in built]:
                self._dismissed.discard(gone)
            for stale in [i for i in self._notice_state if i not in built
                          and i not in self._dismissed]:
                self._notice_state.pop(stale, None)
        return out

    def state(self) -> dict:
        """The whole snapshot. Every read is cached or bounded, so the answer exists with the
        engine dead and the internet absent."""
        engine_snap = self.engine_snapshot()
        corpus_snap = self.corpus_snapshot()
        rows = self._rows(engine_snap)
        corpus_snap["scanned_at"] = _iso(self._scan_cache[0]) if self._scan_cache else None
        # Snapshot under the lock before rendering: `_start_move` inserts while this reads.
        with self._lock:
            tasks = list(self._tasks.values())
        busy = self.acquirer.busy() or any(t.state in ("running", "queued") for t in tasks)
        notices = self._notices(engine_snap, corpus_snap, rows)
        self._last_row_ids = {row["id"] for row in rows}
        working = busy or engine_snap["state"] == "starting"
        return {"ok": True, "product": "siw", "name": "Smol InterWebs", "version": __version__,
                "generated_at": _iso(),
                "poll_after_ms": POLL_WORK_MS if working else POLL_IDLE_MS,
                "server": self.server_snapshot(), "engine": {k: v for k, v in engine_snap.items()
                                                             if not k.startswith("_")},
                "pending_apply": list(self._pending),
                "corpus": corpus_snap,
                "catalog_link": self.offer.get("catalog_link"),
                "rows": rows,
                "tasks": [t.record() for t in tasks],
                "notices": notices}

    # -- the verbs ----------------------------------------------------------------

    def action(self, verb: str, args: dict) -> dict:
        """One verb, checked and run. Raises ConsoleError for the refusals; returns the result
        object the response wraps in `{ok: true, result: ...}`."""
        if verb not in VERBS:
            raise ConsoleError(404, "unknown_verb",
                               f"no verb {verb!r}; known: {', '.join(sorted(VERBS))}")
        allowed = set(VERBS[verb])
        unknown = [k for k in args if k not in allowed]
        if unknown:
            raise ConsoleError(400, "unknown_argument",
                               f"{', '.join(sorted(unknown))} is not an argument of {verb}; "
                               f"it takes: {', '.join(allowed) or 'nothing'}")
        required = set(VERBS[verb]) - OPTIONAL_ARGS.get(verb, set())
        missing = [k for k in required if args.get(k) in (None, "")]
        if missing:
            raise ConsoleError(400, "missing_argument",
                               f"{verb} needs {', '.join(sorted(missing))}")
        handler = getattr(self, f"_verb_{verb}")
        return handler(args)

    def _remember_preview(self, preview: dict) -> None:
        """The gate's memory, capped: one entry per pasted URL used to grow without end."""
        self._previews[preview["url"]] = preview
        while len(self._previews) > PREVIEW_KEEP:
            self._previews.pop(next(iter(self._previews)))

    def _row_by_id(self, row_id: str) -> dict:
        for row in self._rows(self.engine_snapshot()):
            if row["id"] == row_id:
                return row
        raise ConsoleError(404, "unknown_row", f"no row {row_id!r} in the list")

    def _verb_engine_start(self, args: dict) -> dict:
        return self._bring_up(raise_errors=True)

    def _verb_engine_stop(self, args: dict) -> dict:
        with self._engine_lock:  # serialised against start/restart, never two acts at once
            return self._engine_stop_locked(args)

    def _engine_stop_locked(self, args: dict) -> dict:
        record, _where = engine.read_pidfile(self.config)
        if not record:
            raise ConsoleError(409, "foreign_process",
                               "SIW did not start the server on this port — stop it the way it was "
                               "launched, or start one of SIW's")
        # A stop is not a start: cancel any in-flight "starting" window so the page never flaps
        # back to STARTING mid-teardown, and open a "stopping" window for the duration of the
        # (blocking) stop, so a reloaded page still reads STOPPING and keeps Start disabled.
        self._engine_starting_until = 0.0
        self._engine_stopping_until = self._clock() + engine.STOP_BUDGET
        try:
            ok, note = self._stop_engine()
        finally:
            self._engine_stopping_until = 0.0
        if not ok:
            holder = self._listening(int(self.config.port))
            if holder:
                raise ConsoleError(409, "foreign_process",
                                   f"the port is held by pid {holder[0]}, which is not the recorded engine")
        return {"engine": self.engine_snapshot()}

    def _verb_engine_restart(self, args: dict) -> dict:
        # One serialised act: a restart that read the pidfile an in-flight launch had not
        # written yet skipped its stop and launched a second engine.
        with self._engine_lock:
            record, _where = engine.read_pidfile(self.config)
            if record:
                try:
                    self._verb_engine_stop(args)
                except ConsoleError as exc:
                    if exc.status != 409:
                        raise
            return self._verb_engine_start(args)

    def _verb_config_apply(self, args: dict) -> dict:
        if not self._pending:
            return {"pending_apply": [], "engine": self.engine_snapshot()}
        with self._lock:
            changes = list(self._pending)
            self._pending = []
        restarts_server = False
        for change in changes:
            field = change["field"]
            if field in SETTING_FIELDS:
                value = self._secrets.pop(field, change["to"]) if field == "password" else change["to"]
                self.config.set(SETTING_CONFIG_FIELD.get(field, field), value, source="console")
                restarts_server = True
            elif field == "corpus":
                self._runtime_corpus = change["to"]
                self.config.set(field, change["to"], source="console")
                self._scan_cache = None
                try:
                    self.acquirer.set_corpus(change["to"])
                except acquire.AcquireError:
                    pass  # a live transfer keeps writing where it started
            else:
                self.config.set(field, change["to"], source="console")
        try:
            config_module.save(self.config)
        except (OSError, config_module.ConfigError) as exc:
            raise ConsoleError(500, "config_save_failed", f"the config could not be written ({exc})") from exc
        if any(c["requires"] == "engine_restart" for c in changes):
            record, _w = engine.read_pidfile(self.config)
            if record:
                try:
                    self._stop_engine()
                except ConsoleError:
                    pass
        if restarts_server:
            self._reexec_fn()
        return {"pending_apply": [], "engine": self.engine_snapshot()}

    def _verb_config_discard(self, args: dict) -> dict:
        with self._lock:
            self._pending = []
        return {"pending_apply": []}

    def _verb_download(self, args: dict) -> dict:
        row = self._row_by_id(args["id"])
        if row["state"] in ("queued", "downloading"):
            return {"row": row}
        if row["state"] in ("downloaded", "served"):
            return {"row": row}
        corpus_snap = self.corpus_snapshot()
        needed = row["size_bytes"]
        if not row.get("source_url"):
            raise ConsoleError(409, "no_source", f"row {row['id']} has no download source")
        free = corpus_snap["bytes_free"] or 0
        if needed > free:
            raise _NoSpace(f"the download needs {_human(needed)} but only {_human(free)} is free",
                           {"needed": needed, "free": free})
        attempts = self.acquirer.attempts(row["id"])
        self.acquirer.queue(row["id"], row["source_url"], Path(row["source_url"]).name, attempts)
        return {"row": self._row_by_id(args["id"])}

    def _verb_download_all(self, args: dict) -> dict:
        corpus_snap = self.corpus_snapshot()
        free = corpus_snap["bytes_free"] or 0
        started: list[str] = []
        rows = self._rows(self.engine_snapshot())
        total = 0
        for row in rows:
            if row["state"] != "absent" or not row.get("defaulted", True):
                continue
            total += row["size_bytes"]
            started.append(row["id"])
        if total > free:
            raise _NoSpace(f"the default set needs {_human(total)} but only {_human(free)} is free",
                           {"needed": total, "free": free})
        for row in rows:
            if row["id"] in started and row.get("source_url"):
                self.acquirer.queue(row["id"], row["source_url"], _target_name(row["source_url"]),
                                    self.acquirer.attempts(row["id"]))
        return {"started": [r for r in started if any(x["id"] == r for x in rows if x.get("source_url"))]}

    def _verb_cancel(self, args: dict) -> dict:
        self._row_by_id(args["id"])  # the row must exist
        self.acquirer.cancel(args["id"])
        # Re-read on purpose: the row's state is what changed, and the answer must show it.
        row = self._row_by_id(args["id"])
        if row.get("source_url"):
            part = Path(self.config.corpus) / (_target_name(row["source_url"]) + ".part")
            if part.exists():
                row["note"] = "a part is on disk — the next download resumes it"
        return {"row": row}

    def _verb_retry(self, args: dict) -> dict:
        row = self._row_by_id(args["id"])
        if row["state"] != "failed":
            raise ConsoleError(409, "not_failed", f"row {row['id']} is {row['state']}, not failed")
        if not row.get("source_url"):
            raise ConsoleError(409, "no_source", f"row {row['id']} has no download source")
        corpus_snap = self.corpus_snapshot()
        if row["size_bytes"] > (corpus_snap["bytes_free"] or 0):
            raise _NoSpace("not enough free space to retry",
                           {"needed": row["size_bytes"], "free": corpus_snap["bytes_free"] or 0})
        self.acquirer.queue(row["id"], row["source_url"], _target_name(row["source_url"]),
                            self.acquirer.attempts(row["id"]) + 1)
        return {"row": self._row_by_id(args["id"])}

    def _verb_remove(self, args: dict) -> dict:
        row = self._row_by_id(args["id"])
        if row["file"] is None:
            raise ConsoleError(409, "nothing_on_disk", f"row {row['id']} has no file to remove")
        path = Path(row["file"]["path"])
        try:
            path.unlink()
        except OSError as exc:
            raise ConsoleError(500, "remove_failed", f"the file could not be deleted ({exc})") from exc
        self.acquirer.forget(row["id"])
        part = path.with_suffix(path.suffix + ".part")
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        # A paste-added row that is gone for good leaves the list.
        self._extra_rows = [r for r in self._extra_rows if r["id"] != row["id"]]
        self._save_extra()
        self._seen.pop(row["id"], None)
        self._scan_cache = None
        return {"rows": self._rows(self.engine_snapshot())}

    def _verb_scan(self, args: dict) -> dict:
        # `added` is scan-to-scan, against the console's own memory -- a page-render cursor
        # would differ per tab (any /state poll overwrote it, so two tabs reported different
        # additions). The first scan after boot establishes the baseline and reports nothing.
        self.scan(force=True)
        after = {r["id"] for r in self._rows(self.engine_snapshot())}
        added = sorted(after - self._known_rows) if self._known_rows is not None else []
        self._known_rows = after
        corpus_snap = self.corpus_snapshot()
        corpus_snap["scanned_at"] = _iso(self._scan_cache[0]) if self._scan_cache else None
        return {"corpus": corpus_snap, "added": added}

    def _verb_preview(self, args: dict) -> dict:
        url = str(args["url"]).strip()
        if not url.startswith(("http://", "https://")):
            raise ConsoleError(400, "unrecognized_url",
                               "the paste field takes a .zim URL, a .meta4 sidecar URL, or a catalog entry page")
        try:
            preview = resolve_preview(url)
        except ConsoleError:
            raise
        except Exception as exc:
            # The class is the answer; the text is an oracle into the box (file paths,
            # internals of whatever refused) and stays in the server's head.
            raise ConsoleError(503, "preview_unreachable",
                               f"the preview could not be read ({exc.__class__.__name__})") from exc
        self._remember_preview(preview)
        return {"preview": preview}

    def _verb_download_url(self, args: dict) -> dict:
        url = str(args["url"]).strip()
        sha = str(args.get("sha256") or "").strip().lower() or None
        if sha:
            import re as _re
            if not _re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ConsoleError(400, "bad_sha256", "sha256 wants 64 hex digits")
        if sha and url.lower().endswith(".zim"):
            # The user brought the checksum: nothing is fetched, so the preview gate -- which exists
            # against silent unverified fetches -- is satisfied by the preview assembled from the sha.
            preview = _preview_from_sha(url, sha)
        else:
            try:
                preview = resolve_preview(url)
            except ConsoleError:
                raise
            except Exception as exc:
                raise ConsoleError(503, "preview_unreachable",
                                   f"the preview could not be read "
                                   f"({exc.__class__.__name__})") from exc
            if preview["url"] not in self._previews:
                raise ConsoleError(409, "preview_required",
                                   "a download_url must be preceded by a successful preview of the same "
                                   "URL - never a silent fetch")
        self._remember_preview(preview)
        extra = self._extra_rows_from_preview(preview)
        self._extra_rows = [r for r in self._extra_rows if r["id"] != extra["id"]] + [extra]
        self._save_extra()
        self.acquirer.queue(extra["id"], extra["url"], _target_name(extra["url"]), 0,
                            sha256=preview.get("sha256"))
        return {"row": self._row_by_id(extra["id"])}

    def _extra_rows_from_preview(self, preview: dict) -> dict:
        stem = Path(preview["url"]).name
        stem = stem[: -len(".zim")] if stem.endswith(".zim") else stem
        return {"id": stem, "book": corpus.short_name(stem), "title": preview.get("title") or corpus.short_name(stem),
                "build": None, "builds": None, "media": preview.get("media") or "unknown",
                "size_bytes": preview["size_bytes"] or 0, "note": "pasted in through the last tier",
                "defaulted": True, "url": preview["url"]}

    def _verb_set_settings(self, args: dict) -> dict:
        changes = {k: v for k, v in args.items() if v is not None}
        if not changes:
            raise ConsoleError(400, "no_change", "set_settings takes at least one of bind, port, username, password")
        cfg = self.config
        # The field seeds from the port this server answers on (the API port), never from
        # the engine port: seeding from `port` made every save probe the engine's port and
        # answer 409 bind_taken while a healthy engine ran.
        current_port = self._bound[1] if self._bound else int(cfg.api_port)
        new_port = current_port
        if "port" in changes:
            try:
                new_port = int(changes["port"])
            except (TypeError, ValueError):
                raise ConsoleError(400, "bad_port", f"port wants an integer, got {args['port']!r}") from None
            if not 1 <= new_port <= 65535:
                raise ConsoleError(400, "bad_port", f"port wants 1..65535, got {new_port}")
        new_bind = changes.get("bind", cfg.bind)
        new_password = changes.get("password", cfg.password)
        if not is_loopback(new_bind) and not new_password:
            raise ConsoleError(400, "credentials_required",
                               f"binding {new_bind} is beyond loopback and needs a password — "
                               "set one in the same change")
        if "username" in changes or "password" in changes:
            # Only a change that touches a credential field is judged on the pair; the
            # legacy username-set-password-empty config gets its warning at boot, not a
            # veto on unrelated settings.
            new_username = changes.get("username", cfg.username)
            if new_username and not new_password:
                raise ConsoleError(400, "credentials_incomplete",
                                   "a username without a password authenticates nobody — "
                                   "set both in the same change, or clear the username")
        # The port must be free unless it is the port this server already holds, and
        # nothing is probed unless bind or port actually changed.
        if (new_bind, new_port) != (cfg.bind, current_port):
            if self._probe(new_bind, new_port, PROBE_TIMEOUT):
                raise ConsoleError(409, "bind_taken",
                                   f"something already answers on {new_bind}:{new_port}")
        current = {"bind": cfg.bind, "port": int(cfg.api_port), "username": cfg.username, "password": cfg.password}
        for field in ("bind", "port", "username", "password"):
            if field not in changes:
                continue
            label, requires = SETTING_FIELDS[field]
            value = new_port if field == "port" else changes[field]
            # A password never rides the wire in either direction: masked in `from`, masked in
            # `to`, and carried out-of-band to the point of apply.
            shown_from = "***" if (field == "password" and current[field]) else current[field]
            shown_to = "***" if (field == "password" and value) else value
            if field == "password":
                self._secrets[field] = value
            with self._lock:
                self._pending = [p for p in self._pending if p["field"] != field]
                self._pending.append({"field": field, "label": label, "from": shown_from, "to": shown_to,
                                      "requires": requires})
        return {"server": self.server_snapshot(), "pending_apply": list(self._pending)}

    def _verb_set_corpus_dir(self, args: dict) -> dict:
        target = str(args["path"]).strip()
        if not target:
            raise ConsoleError(400, "bad_path", "set_corpus_dir needs a path")
        if not Path(target).is_dir():
            raise ConsoleError(400, "bad_path", f"{target} is not a directory")
        current = self._corpus_dir()
        if current and Path(current).is_dir() and str(Path(current).resolve()) != str(Path(target).resolve()):
            books = self.scan()[0]
            if books and not args.get("move"):
                raise ConsoleError(409, "move_required",
                                   f"the corpus holds {len(books)} books; changing the folder is a move, "
                                   "and a silent re-point would hide them — retry with move=true")
            # A move lands where the names are free. A same-named file at the destination is
            # not the same book (an updated snapshot keeps its name), and replace() would
            # swallow it behind a size check alone — so the move is refused before a byte moves.
            target_dir = Path(target)
            if target_dir.is_dir():
                taken = {p.name.casefold() for p in target_dir.iterdir() if p.is_file()}
                clashing = sorted({Path(b.path).name for b in books
                                   if Path(b.path).name.casefold() in taken})
                if clashing:
                    shown = ", ".join(clashing[:5]) + (f" (+{len(clashing) - 5} more)"
                                                       if len(clashing) > 5 else "")
                    plural = "them" if len(clashing) > 1 else "it"
                    raise ConsoleError(409, "destination_collision",
                                       f"the destination already holds {shown} — {len(clashing)} of its "
                                       f"{len(taken)} file(s) carry the name of a book being moved, and a "
                                       f"move would overwrite {plural}. Free those names there, or choose another folder.")
            task = self._start_move(current, target)
            return {"task": task.record()}
        self._runtime_corpus = target
        self._scan_cache = None
        try:
            self.acquirer.set_corpus(target)
        except acquire.AcquireError as exc:
            raise ConsoleError(409, exc.code, exc.message) from exc
        return {"corpus": self.corpus_snapshot()}

    def _start_move(self, source: str, target: str) -> MoveTask:
        books = self.scan()[0]
        total = sum(b.size for b in books)
        task = MoveTask(id=f"move-{int(self._clock() * 1000)}", source=source, target=target,
                        bytes=total, started=self._clock())
        with self._lock:
            self._tasks[task.id] = task
            finished = [t.id for t in self._tasks.values()
                        if t.state in ("done", "failed", "cancelled")]
            for stale in finished[:-TASK_KEEP]:  # insertion order: keep the newest few
                self._tasks.pop(stale, None)
        thread = threading.Thread(target=self._run_move, args=(task,), daemon=True, name="siw-move")
        thread.start()
        return task

    def _run_move(self, task: MoveTask) -> None:
        Path(task.target).mkdir(parents=True, exist_ok=True)
        try:
            for book in corpus.list_books(task.source):
                if task._cancel.is_set():
                    task.state = "cancelled"
                    return
                name = Path(book.path).name
                done = 0
                tmp = Path(task.target) / (name + ".moving")
                with open(book.path, "rb") as src, open(tmp, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                        done += len(chunk)
                        if task._cancel.is_set():
                            break
                if task._cancel.is_set():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    task.state = "cancelled"
                    return
                promoted = Path(task.target) / name
                tmp.replace(promoted)
                if promoted.stat().st_size != book.size:
                    task.state = "failed"
                    task.error = (f"the copy of {name} is {promoted.stat().st_size} bytes, "
                                  f"the source holds {book.size}; nothing was deleted")
                    return
                try:
                    # A move moves: the copy that agreed on its size takes the bytes off
                    # the source, or the disk silently holds double a 300 GB corpus.
                    Path(book.path).unlink()
                except OSError as exc:
                    task.error = (f"{name} reached the target, but the source could not be "
                                  f"removed ({exc.__class__.__name__}) -- the disk holds both")
                task.done_bytes += book.size
        except OSError as exc:
            task.state = "failed"
            task.error = f"{exc.__class__.__name__}: {exc}"
            return
        task.state = "done"
        # A finished move is a saved change: the corpus field points where the bytes went, and
        # the engine (if any) restarts to serve from there.
        with self._lock:
            self._pending = [p for p in self._pending if p["field"] != "corpus"]
            self._pending.append({"field": "corpus", "label": "Corpus folder", "from": task.source,
                                  "to": task.target, "requires": "engine_restart"})
        self._runtime_corpus = task.target
        self._scan_cache = None
        try:
            self.acquirer.set_corpus(task.target)
        except acquire.AcquireError:
            pass  # a live transfer keeps writing where it started

    def _verb_task_cancel(self, args: dict) -> dict:
        with self._lock:
            task = self._tasks.get(args["id"])
            if task is None:
                raise ConsoleError(404, "unknown_task", f"no task {args['id']!r}")
            if task.state != "running":
                raise ConsoleError(409, "not_running",
                                   f"task {task.id} is {task.state}, not running")
            task._cancel.set()
            return {"tasks": [t.record() for t in list(self._tasks.values())]}

    def _verb_open_folder(self, args: dict) -> dict:
        where = self._corpus_dir()
        if not where:
            raise ConsoleError(400, "no_folder", "there is no corpus folder to open")
        # The folder the config names is created on demand: on a first-run machine it does not
        # exist yet, so the folder is created first.
        try:
            Path(where).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConsoleError(500, "folder_failed", f"the corpus folder could not be created ({exc})") from exc
        try:
            if os.name == "nt":
                os.startfile(where)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", where], **acquire._spawn_flags())
        except (OSError, AttributeError) as exc:
            raise ConsoleError(500, "open_failed", f"the file manager could not be opened ({exc})") from exc
        return {"opened": where}

    def _verb_dismiss(self, args: dict) -> dict:
        self._dismissed.add(args["id"])
        self._notice_state.pop(args["id"], None)
        return {"notices": []}

    # -- process acts ------------------------------------------------------------

    def _reexec(self) -> None:
        """A server field applied: relaunch this process on the new values. On Windows there is
        no exec, so the pattern is spawn-then-exit: the child inherits the terminal, takes the
        port when it binds, and this process leaves."""
        argv = [sys.executable] + sys.argv
        # The child is not a plain start: an apply's relaunch must not open a browser tab.
        env = {**os.environ, "SIW_REEXEC": "1"}
        try:
            if os.name == "nt":
                subprocess.Popen(argv, env=env, **acquire._spawn_flags())
            else:
                os.execve(sys.executable, argv, env)
        finally:
            os._exit(0)

    # -- paste rows persistence ----------------------------------------------------

    def _load_extra(self) -> None:
        try:
            raw = json.loads(self._extra_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, json.JSONDecodeError, AttributeError):
            return
        if isinstance(raw, list):
            # The file is user state, but it is also an attack surface once tampered: a
            # row whose url is not an http(s) URL never becomes a fetchable target.
            self._extra_rows = [r for r in raw
                                if isinstance(r, dict) and r.get("id")
                                and str(r.get("url") or "").startswith(("http://", "https://"))]

    def _save_extra(self) -> None:
        if self._extra_path is None:
            return
        try:
            self._extra_path.parent.mkdir(parents=True, exist_ok=True)
            self._extra_path.write_text(json.dumps(self._extra_rows, indent=1) + "\n", encoding="utf-8")
        except OSError:
            pass  # persistence is a courtesy; a row that only lives in memory still works this run


def _extra_rows_path(config) -> Path | None:
    if config.path:
        return config.path.parent / "siw-pasted.json"
    return config_module.machine_config_dir() / "siw-pasted.json"


def _human(nbytes: int) -> str:
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024 ** 3:.1f} GB"
    return f"{nbytes // 1024 // 1024} MB"


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _real_probe(config):
    def probe(host: str, port: int, timeout: float) -> bool:
        return engine.probe(host, port, timeout=timeout)
    return probe


def default_webdir() -> Path:
    return Path(__file__).resolve().parent.parent / "web"


def resolve_preview(url: str) -> dict:
    """The paste field's gate: a URL in, the preview out.

    Three shapes, one answer. A `.meta4` URL is the sidecar itself; a `.zim` URL gets its
    sidecar; anything else is a catalog entry page, fetched once (with the confirmation cookie
    its bot-wall asks for) and read for the download link on it. The preview reads the
    sidecar's size and sha-256 -- the checksum of record -- and names the book from the offered
    set and the shipped scan where either knows it. Nothing is downloaded; nothing is written."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConsoleError(400, "unrecognized_url", "the paste field takes an http(s) URL")
    kind = "zim"
    zim_url = url
    if url.endswith(".meta4"):
        sidecar = url
        zim_url = url[: -len(".meta4")]
    elif url.lower().endswith(".zim"):
        sidecar = url + ".meta4"
    else:
        kind = "catalog_entry"
        page = _fetch_page(url)
        import re
        match = re.search(r"https?://[^\s\"'<>]+?\.zim(?:\.meta4)?", page)
        if not match:
            raise ConsoleError(503, "preview_unreachable",
                               "the page carried no kiwix download link — paste the .zim URL instead")
        candidate = match.group(0)
        sidecar = candidate if candidate.endswith(".meta4") else candidate + ".meta4"
        zim_url = candidate[: -len(".meta4")] if candidate.endswith(".meta4") else candidate
    meta = acquire.fetch_meta4(sidecar)
    name = Path(zim_url).name
    stem = name[: -len(".zim")] if name.endswith(".zim") else name
    offer = _match_offered(stem)
    profile = _match_profile(stem)
    source = offer or profile or {}
    return {"kind": kind, "url": zim_url, "name": stem,
            "title": source.get("title") or None,
            "size_bytes": meta.get("size") or 0,
            "sha256": meta.get("sha256"),
            "checksum_source": "meta4" if meta.get("sha256") else None,
            "book": source.get("book") or corpus.short_name(stem),
            "build": source.get("build"),
            "media": source.get("media") or "unknown",
            "warning": (None if meta.get("sha256") else
                        "the sidecar carries no checksum — the download will be unverified")}


# The preview fetch's bounds: this verb fetches any http(s) URL the operator pastes,
# including hosts on the local network, so its answer is capped in bytes, redirects and
# time. The capability itself is stated in web/CONTRACT.md and SECURITY.md.
PAGE_CAP = 1024 * 1024
PAGE_TIMEOUT = 30.0
PAGE_REDIRECT_CAP = 3


class _CappedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = PAGE_REDIRECT_CAP


def _target_name(url: str) -> str:
    """The file a URL downloads to: the percent-decoded basename with the query gone
    and the characters Windows forbids removed. `.../a.zim?x=1` must not arrive as a name
    the OS refuses -- it used to, and the failure surfaced from inside the child process."""
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit("/", 1)[-1]
    name = name.rsplit(chr(92), 1)[-1]
    cleaned = "".join(c for c in name if c not in '<>:"|?' and ord(c) >= 32).rstrip(".")
    return cleaned or "download.zim"


def _fetch_page(url: str) -> str:
    """One bounded GET of a pasted page, with the confirmation cookie its bot-wall sets.
    The page is data in, not a command out: nothing it carries is executed, only its link
    is read, and the redirect chain it can name is capped."""
    headers = {"User-Agent": "siw/preview", "Cookie": "confirmed=yes"}
    opener = urllib.request.build_opener(_CappedRedirectHandler())
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=PAGE_TIMEOUT) as reply:
        return reply.read(PAGE_CAP)[:PAGE_CAP].decode("utf-8", "replace")


def _match_offered(stem: str) -> dict | None:
    offer = load_offer()
    for book in offer["books"]:
        file = book.get("file", "")
        if Path(file).stem.lower() == stem.lower():
            return book
    return None


def _match_profile(stem: str) -> dict | None:
    profiles = load_profiles()
    record = profiles["by_name"].get(stem.lower())
    if not record:
        return None
    return {"title": record["title"], "book": corpus.short_name(stem), "build": None,
            "media": record["media"]}


def _preview_from_sha(url: str, sha: str) -> dict:
    """The no-sidecar path: the user brings the checksum, so nothing is fetched at all -- the
    preview is assembled from the URL and whatever the shipped profiles know about the stem."""
    name = _target_name(url)
    stem = name[: -len(".zim")] if name.endswith(".zim") else name
    source = _match_offered(stem) or _match_profile(stem) or {}
    return {"kind": "zim", "url": url, "name": stem, "title": source.get("title") or None,
            "size_bytes": 0, "sha256": sha, "checksum_source": "user",
            "book": source.get("book") or corpus.short_name(stem), "build": source.get("build"),
            "media": source.get("media") or "unknown", "warning": None}


class ConsoleHandler(api.Handler):
    """The one HTTP surface. The console routes are answered here with their own headers --
    the page is same-origin (no CORS header, there is nothing cross-origin), and the kernel's
    routes pass through untouched. If credentials are set, every route on the port asks for
    them, before anything else is answered: one port is one credential state.

    The handler keeps no state of its own: the console, its dispatch, and the config hang off
    the server object (the kernel's `service` attribute, and `console_service` beside it), the
    way the kernel's handler already finds its service."""

    def _cfg(self):
        return self.server.service.config  # type: ignore[attr-defined]

    def _cs(self):
        return self.server.console_service  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        """Answered with a 401 (and the challenge) when the credentials are wanted and absent
        or wrong. The check is constant-time on the password."""
        cfg = self._cfg()
        if not cfg.password:
            return True
        ok = _basic_auth_ok(self.headers.get("Authorization") or "", cfg.username, cfg.password)
        if not ok:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="siw"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        return ok

    def log_message(self, *args) -> None:
        """The kernel's API stays quiet (an API polled by an advisor would drown the terminal),
        but this window is the console — a place people look at — and a server that prints
        nothing reads as dead. So every hit gets its line here, one per request; `SIW_VERBOSE=0`
        keeps the kernel's silence for the run that must be quiet. The call skips the kernel's
        layer on purpose: its gate is the inverse of this one."""
        if os.environ.get("SIW_VERBOSE") == "0":
            return
        super(api.Handler, self).log_message(*args)

    def _same_origin_write(self) -> bool:
        """Guard the write verbs against the browser. A cross-origin page reaches this port
        only through a browser, and a browser always says so: `Origin` names the foreign
        site on every cross-origin POST, and `Sec-Fetch-Site` says it even when the page
        has no address to name. A client that is not a browser (curl, an adapter, the CLI)
        sends neither and is unconstrained. The console page also sends `X-Requested-With:
        siw`; a plain form post cannot carry that header, which is the point of it."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").strip()
        host = (self.headers.get("Host") or "").strip().lower()
        cross = site in ("cross-site", "cross-origin") or (
            bool(origin) and origin.lower() not in (f"http://{host}", f"https://{host}"))
        if cross:
            self._send_json(403, {"ok": False, "error": {
                "code": "cross_origin",
                "message": "this port takes writes only from its own origin"}})
            return False
        return True

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self) -> None:
        index = self.server.console.webdir / "index.html"  # type: ignore[attr-defined]
        try:
            body = index.read_bytes()
        except OSError:
            body = ("<!doctype html><h1>the console page is missing</h1>"
                    f"<p>expected {index}</p>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_page()
        elif path == "/state":
            self._send_json(200, self._cs().state())
        else:
            super().do_GET()

    def do_PUT(self):  # noqa: N802
        # One port, one credential state: the kernel answers PUT with a 405, but behind
        # the password first -- the invariant is the answer, not the verb.
        if not self._authorized():
            return
        super().do_PUT()

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        if not self._same_origin_write():
            return
        path = self.path.split("?", 1)[0]
        if path == "/mcp":
            self._send_mcp()
            return
        if path != "/action":
            super().do_POST()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > BODY_CAP:
                self._send_json(413, {"ok": False, "error": {
                    "code": "body_too_large",
                    "message": f"request bodies are capped at {BODY_CAP} bytes"}})
                return
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            verb = str(payload.get("verb") or "")
            args = payload.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError
        except (OSError, ValueError):
            self._send_json(400, {"ok": False,
                                  "error": {"code": "bad_request",
                                            "message": "the body wants {verb, args}"}})
            return
        status, body = self._cs().action(verb, args)
        self._send_json(status, body)

    def _send_mcp(self):
        """The agent's front door: JSON-RPC 2.0 over this port, behind the same credential check as
        everything else here. A notification gets a 202 with no body; a request gets its JSON-RPC
        answer; the kernel's search and the console's verbs are what the tools call, so there is no
        second retrieval path to drift."""
        from siw import mcp
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > BODY_CAP:
                self._send_json(413, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600, "message": f"request bodies are capped at {BODY_CAP} bytes"}})
                return
            raw = self.rfile.read(length) if length else b"null"
            payload = json.loads(raw.decode("utf-8") or "null")
        except (OSError, ValueError):
            # A parse failure is the JSON-RPC parse error, not a console error code.
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "parse error: /mcp wants a JSON-RPC 2.0 body"}})
            return
        try:
            response = mcp.handle(payload, self._cs())
        except Exception as exc:  # -32603: an error object, not a dropped connection
            self._send_json(200, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32603,
                "message": f"internal error: {exc.__class__.__name__}"}})
            return
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, response)


def _boot_banner(version: str, bound_host: str, bound_port: int, cfg) -> str:
    """The console window's first words: where the console answers, what it manages, and how to
    leave. One fact per line — the window is a place to look at, not a stream to parse."""
    base = f"http://{bound_host}:{bound_port}"
    return (
        f"siw {version}: console {base} (engine {cfg.host}:{cfg.port}, corpus {cfg.corpus})\n"
        f"  spec:   {base}/spec\n"
        f"  health: {base}/health\n"
        f"  Ctrl-C stops it"
    )


def _open_console_url(cfg, env: dict | None = None) -> str | None:
    """Where a plain start opens the browser, or None: the config says not to, or this process
    is the re-exec of an applied config — which is not a plain start and must not steal a tab."""
    if not cfg.open_browser:
        return None
    if (env if env is not None else os.environ).get("SIW_REEXEC"):
        return None
    return f"http://127.0.0.1:{int(cfg.api_port)}/"


def _open_console(url: str) -> None:
    """A beat after the server answers, so the tab lands on a page that is up."""
    import webbrowser
    time.sleep(1.0)
    webbrowser.open(url)


def serve(config=None) -> int:
    """The console server's main loop: the kernel's routes and the console's routes on one
    port, one credential state. A first run lands in the machine's data directory and writes
    its config -- zero prompts."""
    cfg = config if config is not None else config_module.resolve()[0]
    # The credential rule, enforced before the socket exists: a non-loopback bind with no
    # password is refused at boot, whichever layer named the bind (config file, SIW_BIND,
    # or --bind). This is the rule web/CONTRACT.md states.
    if not is_loopback(cfg.bind) and not cfg.password:
        print(f"siw: refusing to bind {cfg.bind}:{int(cfg.api_port)} without a password -- "
              "a reachable console is a share of this machine. Set a password (the Settings "
              "pane, the config file, or SIW_PASSWORD) and start again.", file=sys.stderr)
        return 2
    if cfg.username and not cfg.password:
        print("siw: warning: a username with no password authenticates nobody -- the "
              "Settings pane wants both credentials, or neither.", file=sys.stderr)
    corpus_dir = Path(cfg.corpus) if cfg.corpus else config_module.machine_config_dir() / "corpus"
    if not corpus_dir.is_dir():
        corpus_dir.mkdir(parents=True, exist_ok=True)
    if cfg.corpus is None or str(Path(cfg.corpus).resolve()) != str(corpus_dir.resolve()):
        cfg.set("corpus", str(corpus_dir), source="first_run")
    if cfg.path is None:
        cfg.path = config_module.machine_config_dir() / "config.json"
        config_module.save(cfg)
    console = Console(cfg, webdir=default_webdir())
    service = api.Service(cfg)
    console_service = ConsoleService(console, cfg)
    # The kernel's make_server hardwires its own Handler, which answers the kernel's routes and
    # nothing else: the console's routes need the ConsoleHandler, so the server is bound here,
    # the same way, with `ConsoleHandler` bound to the socket.
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer((cfg.bind, int(cfg.api_port)), ConsoleHandler)
    server.daemon_threads = True
    server.service = service  # type: ignore[attr-defined]
    server.console_service = console_service  # type: ignore[attr-defined]
    server.console = console  # type: ignore[attr-defined]
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    console.set_bound(bound_host, bound_port)
    print(_boot_banner(__version__, bound_host, bound_port, cfg), flush=True)
    # The console opens itself when the config says so: one place for the decision (not the
    # front-door scripts) and one URL (the port it just bound, not a remembered one).
    url = _open_console_url(cfg)
    if url:
        threading.Thread(target=_open_console, args=(url,), daemon=True, name="siw-open").start()
    # A plain start means a working server: probe, and if the port is dark, bring the engine
    # up the way the Start verb would (fetching the pinned binary first when the config names
    # none). A foreign engine on the port is never touched — the probe says up, and nothing happens.
    def _boot() -> None:
        try:
            if not console._probe(cfg.host, int(cfg.port), PROBE_TIMEOUT):
                console._bring_up(raise_errors=False)
        except Exception:
            pass  # the next /state carries the parked error

    threading.Thread(target=_boot, name="siw-boot", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nsiw: leaving", flush=True)
    finally:
        console.acquirer.stop()
        server.server_close()
    return 0


class _NoSpace(ConsoleError):
    """The 409 that carries numbers: `{needed, free}` ride the error object, not the message."""

    def __init__(self, message: str, extra: dict):
        super().__init__(409, "no_space", message)
        self.extra = extra


class ConsoleService:
    """The dispatch the HTTP layer calls: `/state` out, a verb in, one JSON answer shape.

    Kept separate from the socket (like the kernel's `Service`) so the test suite asserts the
    whole wire contract without a port."""

    def __init__(self, console: Console, config):
        self.console = console
        self.config = config

    def state(self) -> dict:
        return self.console.state()

    def action(self, verb: str, args: dict) -> tuple[int, dict]:
        try:
            result = self.console.action(verb, args)
            return 200, {"ok": True, "result": result}
        except _NoSpace as exc:
            return exc.status, {"ok": False,
                                "error": {"code": exc.code, "message": exc.message, **exc.extra}}
        except acquire.AcquireError as exc:
            return 409, {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except ConsoleError as exc:
            return exc.status, {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # a verb that raises is a server bug, answered as one
            return 500, {"ok": False, "error": {"code": "crash",
                                                 "message": f"{exc.__class__.__name__}: {exc}"}}
