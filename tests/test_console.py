"""The console's wire battery: the whole snapshot and every verb, over fakes.

No socket, no network, no kiwix-serve, no curl: the probe, the catalog, the spawn, the stop,
and the clock are all injected, so a state the backend must show (engine down, a download
failing, a row just verified) is *made* and then *read back through the wire shape*. The
battery asserts the contract's key sets exactly -- a field the contract does not name is as
much a bug as one it does, because the page and every adapter code against that set.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import threading as _threading
import time as _time
import urllib.error
import urllib.request

import pytest

from siw import acquire, config as config_module
from siw.console import (Console, ConsoleService, _basic_auth_ok, _human, _match_offered,
                         _match_profile, is_loopback, load_offer, load_profiles)

# The contract's exact key sets. A snapshot that carries one more field, or loses one, is a
# shape change the adapters were not told about.
ENGINE_KEYS = {"state", "up", "host", "port", "url", "exe", "version", "form", "bound_on",
               "started", "pid", "started_by_siw", "log_path", "uptime_seconds", "served_books",
               "configured_books", "drift", "problem", "last_checked", "bin"}
BIN_KEYS = {"state", "version", "path", "exists", "progress", "error"}
SERVER_KEYS = {"bind", "port", "url", "loopback", "bound_on", "credentials", "warning",
               "uptime_seconds", "config_path", "platform"}
CORPUS_KEYS = {"directory", "exists", "bytes_free", "bytes_total", "books", "bytes", "parts",
               "scanned_at", "scan_errors"}
ROW_KEYS = {"id", "book", "title", "build", "builds", "media", "size_bytes", "state", "note",
            "defaulted", "ident", "file", "served", "progress", "error", "source_url", "actions"}
STATE_KEYS = {"ok", "product", "name", "version", "generated_at", "poll_after_ms", "server",
              "engine", "pending_apply", "corpus", "catalog_link", "rows", "tasks", "notices"}
IDENT_KEYS = {"rung", "name", "title", "date", "confidence"}
FILE_KEYS = {"name", "path", "bytes", "mtime", "sha256", "verified"}
SERVED_KEYS = {"configured", "confirmed", "seen_at", "url", "page_url"}
PROGRESS_KEYS = {"percent", "bytes", "total_bytes", "rate_bytes_s", "eta_seconds",
                 "queue_position", "url"}
ERROR_KEYS = {"code", "message", "retryable", "attempts", "failed_at", "partial_removed"}
NOTICE_KEYS = {"id", "level", "since", "text", "action"}
PENDING_KEYS = {"field", "label", "from", "to", "requires"}
PREVIEW_KEYS = {"kind", "url", "name", "title", "size_bytes", "sha256", "checksum_source",
                "book", "build", "media", "warning"}
TASK_KEYS = {"id", "kind", "label", "state", "bytes", "done_bytes", "rate_bytes_s",
             "eta_seconds", "from", "to", "error"}

WIKI = "wikipedia_en_all_mini_2026-09"        # the offered wikipedia file (mini build)
WIKI_ID = "wikipedia/mini"
PY = "devdocs_en_python_2026-10"              # the offered python docs file
PY_ID = "devdocs python"
RUST_ID = "devdocs rust"
CPP_ID = "devdocs cpp"
JS_ID = "devdocs javascript"
CPP_FILE = "devdocs_en_cpp_2026-10.zim"
RUST_FILE = "devdocs_en_rust_2026-10.zim"
PY_FILE = "devdocs_en_python_2026-10.zim"


class FakeTools:
    """The binary fetch as the console's seam sees it: a state the test drives by hand."""

    def __init__(self):
        self.state = "missing"
        self.path = None
        self.error = None
        self.progress = None
        self.exists = False
        self.ons: list = []

    def snapshot(self):
        return {"state": self.state, "version": "3.8.1", "path": self.path,
                "exists": self.exists, "progress": self.progress, "error": self.error}

    def start(self):
        self.state = "fetching"
        self.progress = {"received": 0, "total": 1000}

    def set_on_ready(self, cb):
        self.ons.append(cb)


class FakeEngine:
    """The engine as the console's seams see it: a switch, a catalog, a launch record."""

    def __init__(self, now: float):
        self.now = now
        self.up = False
        self.entries: list[dict] = []
        self.problem = ""
        self.launches: list[list[str]] = []
        self.stops: list[str] = []
        self.reexecs: list[str] = []

    def probe(self, host, port, timeout=3.0):
        return self.up

    def catalog(self):
        return list(self.entries), self.problem

    def listening(self, port):
        return [9999] if self.up else []

    def version(self, exe):
        return ("kiwix-serve 3.8.1 (fake)", "fake")

    def launch(self, books):
        self.launches.append(sorted(b.name for b in books))
        return (4242, "paths", 0.5)

    def stop(self):
        self.stops.append("stop")
        return (True, "stopped (fake)")


@pytest.fixture
def fake():
    return FakeEngine(now=1_700_000_000.0)


@pytest.fixture
def corpus_dir(tmp_path):
    folder = tmp_path / "zims"
    folder.mkdir()
    # Two fake books, under the offered set's own names: the scan finds them and the offer
    # claims them, so their rows are offer rows with files.
    (folder / WIKI).with_suffix(".zim").write_bytes(b"0" * 512)
    (folder / PY).with_suffix(".zim").write_bytes(b"0" * 256)
    return folder


@pytest.fixture
def cfg(corpus_dir, tmp_path):
    return config_module.Config(
        {"corpus": str(corpus_dir), "port": 19191, "api_port": 19192,
         "host": "127.0.0.1", "bind": "127.0.0.1",
         "exe": str(tmp_path / "kiwix-serve"), "username": "op"},
        path=tmp_path / "siw.json")


@pytest.fixture
def acq(corpus_dir):
    return acquire.Acquirer(corpus_dir, run_worker=False)


@pytest.fixture
def tools():
    return FakeTools()


@pytest.fixture
def console(cfg, acq, fake, tmp_path, tools):
    c = Console(cfg, offer=load_offer(), profiles=load_profiles(), acquirer=acq,
                probe=fake.probe, catalog=fake.catalog, listening=fake.listening,
                version=fake.version, launch=fake.launch, stop_engine=fake.stop,
                clock=lambda: fake.now, reexec=lambda: fake.reexecs.append("reexec"),
                fetcher=tools)
    c._extra_path = tmp_path / "siw-pasted.json"  # keep the battery from touching machine state
    return c


def row_by_id(state, row_id):
    for row in state["rows"]:
        if row["id"] == row_id:
            return row
    raise AssertionError(f"no row {row_id} in {[r['id'] for r in state['rows']]}")


def act(console_, verb, **args):
    return ConsoleService(console_, console_.config).action(verb, args)


def _queue(console_, row_id, file, state, **kw):
    url = f"https://lb.download.kiwix.org/z/{file}"
    t = acquire.Transfer(id=row_id, url=url, file=file, corpus=pathlib.Path(console_.config.corpus), **kw)
    t.state = state
    console_.acquirer._transfers[row_id] = t
    console_.acquirer._order.append(row_id)
    return t


# -- the snapshot --------------------------------------------------------------------

def test_state_shape_exact(console):
    state = console.state()
    assert set(state) == STATE_KEYS
    assert state["ok"] is True and state["product"] == "siw"
    assert set(state["server"]) == SERVER_KEYS
    assert set(state["server"]["credentials"]) == {"mode", "configured", "username"}
    assert set(state["engine"]) == ENGINE_KEYS
    assert set(state["engine"]["bin"]) == BIN_KEYS
    assert set(state["corpus"]) == CORPUS_KEYS
    assert state["catalog_link"] == "https://browse.library.kiwix.org"
    assert state["tasks"] == [] and state["pending_apply"] == []
    assert len(state["rows"]) == 9  # the offered set, every one of them a row
    for row in state["rows"]:
        assert set(row) == ROW_KEYS
        if row["ident"] is not None:
            assert set(row["ident"]) == IDENT_KEYS
        if row["file"] is not None:
            assert set(row["file"]) == FILE_KEYS
        if row["served"] is not None:
            assert set(row["served"]) == SERVED_KEYS
        if row["progress"] is not None:
            assert set(row["progress"]) == PROGRESS_KEYS
        if row["error"] is not None:
            assert set(row["error"]) == ERROR_KEYS
    for notice in state["notices"]:
        assert set(notice) == NOTICE_KEYS


def test_state_with_engine_down(console):
    state = console.state()
    eng = state["engine"]
    assert eng["state"] == "down" and eng["up"] is False
    assert eng["served_books"] is None and eng["drift"] is None
    assert eng["problem"]  # the down reason is a sentence, not a null
    assert eng["pid"] is None and eng["started_by_siw"] is False
    # A book on disk with no engine is `downloaded`, not `served`: serving is a confirmation.
    row = row_by_id(state, WIKI_ID)
    assert row["state"] == "downloaded"
    assert row["served"]["configured"] is True and row["served"]["confirmed"] is False
    assert row["served"]["url"] is None and row["served"]["seen_at"] is None
    assert row["file"]["bytes"] == 512 and row["file"]["verified"] is None
    assert row["ident"]["rung"] == "filename" and row["ident"]["confidence"] == "high"
    # And the page tells you so, with the act to do about it.
    notice = next(n for n in state["notices"] if n["id"] == "engine-down")
    assert notice["level"] == "warn"
    assert notice["action"] == {"verb": "engine_start", "label": "Start engine"}
    # The offered row that is not on disk is absent, with its download action.
    absent = row_by_id(state, RUST_ID)
    assert absent["state"] == "absent" and absent["actions"] == ["download"]
    assert absent["source_url"].startswith("https://lb.download.kiwix.org/")
    assert absent["file"] is None and absent["served"] is None and absent["ident"] is None


def test_state_with_engine_up_confirmed(console, fake):
    fake.up = True
    fake.entries = [
        {"id": "u1", "name": "Wikipedia", "title": "Wikipedia", "language": "en",
         "article_count": "123", "content_name": WIKI},
        {"id": "u2", "name": "DevDocs", "title": "DevDocs", "language": "en",
         "article_count": "456", "content_name": PY},
    ]
    state = console.state()
    eng = state["engine"]
    assert eng["state"] == "up" and eng["up"] is True
    assert eng["served_books"] == 2 and eng["configured_books"] == 2
    assert eng["drift"]["configured_only"] == [] and eng["drift"]["served_only"] == []
    assert eng["version"].startswith("kiwix-serve")
    assert eng["problem"] is None
    row = row_by_id(state, WIKI_ID)
    assert row["state"] == "served"
    assert row["served"]["confirmed"] is True
    assert row["served"]["seen_at"]  # a confirmation is stamped
    assert row["served"]["url"] == f"http://127.0.0.1:19191/content/{WIKI}/"
    assert row["served"]["page_url"] == row["served"]["url"]
    assert state["notices"] == []  # an up engine with its configured books warns nothing
    assert state["poll_after_ms"] == 5000  # idle: the slow tick


def test_foreign_corpus_notice(console, fake):
    # An engine that serves more books than the corpus directory accounts for was started
    # elsewhere, against a corpus this config does not name. The page must say so instead of
    # showing a wall of ABSENT rows with no explanation (a plain `siw.bat` on a machine with
    # a live kiwix-serve is exactly this shape).
    fake.up = True
    fake.entries = [
        {"id": f"z{i}", "name": f"Book {i}", "title": f"Book {i}", "language": "en",
         "article_count": "1", "content_name": f"book{i}"}
        for i in range(5)
    ]
    state = console.state()
    assert state["engine"]["served_books"] == 5
    assert state["engine"]["configured_books"] < 5
    notice = next(n for n in state["notices"] if n["id"] == "foreign-corpus")
    assert notice["level"] == "warn"
    assert "5" in notice["text"]


def test_engine_start_fetches_a_missing_binary(console, fake, cfg, tools):
    # No exe configured, no binary fetched: Start is the fetch. The act answers 200 with the
    # fetch in flight, spawns nothing, and the landed binary starts the engine itself.
    cfg.set("exe", None, source="test")
    status, body = act(console, "engine_start")
    assert status == 200 and body["result"]["engine"]["bin"]["state"] == "fetching"
    assert fake.launches == [] and cfg.exe is None
    # The page shows the act as the act: a fetch, not a complaint.
    assert "fetching kiwix-serve 3.8.1" in body["result"]["engine"]["problem"]
    # The fetch lands: the hand-off starts the engine, and the config's exe is the landed binary.
    tools.state, tools.path, tools.exists = "ok", str(pathlib.Path(cfg.corpus) / "kiwix-serve"), True
    tools.ons[0]()
    assert fake.launches == [[PY, WIKI]]
    assert cfg.exe == tools.path
    # And the wire remembers who put it there: a user-named exe is `configured`, the one the
    # fetch installed is `fetched` — the runtime section shows the distinction.
    assert console.state()["engine"]["bin"]["state"] == "fetched"


def test_engine_start_waits_for_an_in_flight_fetch(console, cfg, tools):
    cfg.set("exe", None, source="test")
    tools.state, tools.progress = "fetching", {"received": 400, "total": 1000}
    status, body = act(console, "engine_start")
    assert status == 409 and body["error"]["code"] == "binary_fetching"


def test_failed_binary_fetch_reports_and_retries(console, cfg, tools):
    cfg.set("exe", None, source="test")
    tools.state, tools.error, tools.progress = "failed", "OSError: sha256 mismatch", None
    state = console.state()
    assert "sha256 mismatch" in state["engine"]["problem"]
    assert any(n["id"] == "binary_failed" for n in state["notices"])
    status, body = act(console, "engine_start")  # the retry: the fetch starts again
    assert status == 200 and body["result"]["engine"]["bin"]["state"] == "fetching"


def test_configured_binary_missing_warns(console, cfg, tools):
    # exe is a user's choice: SIW does not fetch over it — it says the path is not on disk.
    cfg.set("exe", str(pathlib.Path(cfg.corpus) / "no-such-kiwix-serve"), source="test")
    state = console.state()
    assert state["engine"]["bin"]["state"] == "configured"
    assert state["engine"]["bin"]["exists"] is False
    assert any(n["id"] == "binary_missing" for n in state["notices"])


def test_scanned_row_beyond_the_offer(console):
    # A book in the folder the offer does not claim is a row: scanned identity, no source url.
    # A different build than the offered one, and no profile file ships, so the stem is
    # unknown: filename rung, low confidence, no title (a user-supplied profile would know it).
    (pathlib.Path(console.config.corpus) / "wikipedia_en_all_maxi_2026-08.zim").write_bytes(b"1" * 64)
    console.scan(force=True)
    state = console.state()
    row = row_by_id(state, "wikipedia_en_all_maxi_2026-08")
    assert row["state"] == "downloaded"
    assert row["ident"]["rung"] == "filename"
    assert row["ident"]["confidence"] == "low"
    assert row["ident"]["title"] is None
    assert row["ident"]["date"] == "2026-08"
    assert row["source_url"] is None
    assert row["actions"] == ["remove", "scan"]
    # Removing it makes the row leave the list: there is no offer row to fall back to.
    status, body = act(console, "remove", id=row["id"])
    assert status == 200
    assert row["id"] not in [r["id"] for r in body["result"]["rows"]]


def test_scanned_row_unknown_stem_is_low_confidence(console):
    (pathlib.Path(console.config.corpus) / "mystery_zim_2020-01.zim").write_bytes(b"2" * 32)
    console.scan(force=True)
    row = row_by_id(console.state(), "mystery_zim_2020-01")
    assert row["ident"]["rung"] == "filename"
    assert row["ident"]["confidence"] == "low"
    assert row["ident"]["date"] == "2020-01"


# -- the row states through the queue --------------------------------------------------

def test_row_states_and_actions(console):
    _queue(console, RUST_ID, RUST_FILE, acquire.QUEUED)
    rust = row_by_id(console.state(), RUST_ID)
    assert rust["state"] == "queued" and rust["actions"] == ["cancel"]
    assert rust["progress"]["queue_position"] >= 0
    assert rust["progress"]["url"].endswith(RUST_FILE)

    _queue(console, CPP_ID, CPP_FILE, acquire.DOWNLOADING)
    part = pathlib.Path(console.config.corpus) / (CPP_FILE + ".part")
    part.write_bytes(b"0" * 128)
    cpp = row_by_id(console.state(), CPP_ID)
    assert cpp["state"] == "downloading" and cpp["actions"] == ["cancel"]
    assert cpp["progress"]["bytes"] == 128

    _queue(console, JS_ID, "devdocs_en_javascript_2026-10.zim", acquire.FAILED,
           error={"code": "checksum", "message": "the hash did not match", "retryable": True})
    js = row_by_id(console.state(), JS_ID)
    assert js["state"] == "failed"
    assert js["error"]["code"] == "checksum" and js["error"]["retryable"] is True
    assert js["error"]["attempts"] == 0
    assert js["actions"] == ["retry", "remove"]
    assert console.state()["poll_after_ms"] == 2000  # a queue is work: the fast tick


# -- the verbs: guards ------------------------------------------------------------------

def test_unknown_verb_names_the_known(console):
    status, body = act(console, "fly")
    assert status == 404 and body["ok"] is False
    assert body["error"]["code"] == "unknown_verb"
    assert "engine_start" in body["error"]["message"]
    assert "download_all" in body["error"]["message"]


def test_unknown_argument_is_refused(console):
    status, body = act(console, "download", id=RUST_ID, extra=1)
    assert status == 400 and body["error"]["code"] == "unknown_argument"
    assert "id" in body["error"]["message"]  # it names what the verb takes


def test_missing_argument_is_refused(console):
    status, body = act(console, "download")
    assert status == 400 and body["error"]["code"] == "missing_argument"


def test_unknown_row_is_refused(console):
    status, body = act(console, "cancel", id="never_heard_of_it")
    assert status == 404 and body["error"]["code"] == "unknown_row"


# -- the verbs: the download machine ----------------------------------------------------

def test_download_queues_and_is_idempotent(console):
    status, body = act(console, "download", id=RUST_ID)
    assert status == 200
    row = body["result"]["row"]
    assert row["state"] == "queued" and row["actions"] == ["cancel"]
    status2, body2 = act(console, "download", id=RUST_ID)
    assert status2 == 200
    assert console.acquirer._order.count(RUST_ID) == 1  # one queue entry, not two


def test_download_refused_without_space(console):
    for row in console.offer["books"]:
        if row["id"] == "ifixit":
            row["size_bytes"] = 10**15
    status, body = act(console, "download", id="ifixit")
    assert status == 409 and body["error"]["code"] == "no_space"
    assert body["error"]["needed"] == 10**15
    assert body["error"]["free"] < body["error"]["needed"]


def test_download_all_starts_the_default_set(console):
    on_disk = {p.name for p in pathlib.Path(console.config.corpus).iterdir()}
    started = act(console, "download_all")[1]["result"]["started"]
    expected = [b["id"] for b in console.offer["books"]
                if b.get("defaulted", True) and b["file"] not in on_disk]
    assert started == expected
    for row_id in expected:
        assert console.acquirer.state_of(row_id) == "queued"


def test_download_all_refused_without_space(console):
    for row in console.offer["books"]:
        row["size_bytes"] = 10**15
    status, body = act(console, "download_all")
    assert status == 409 and body["error"]["code"] == "no_space"
    assert console.acquirer._order == []  # nothing half-started


def test_cancel_leaves_the_part(console):
    _queue(console, RUST_ID, RUST_FILE, acquire.DOWNLOADING)
    part = pathlib.Path(console.config.corpus) / (RUST_FILE + ".part")
    part.write_bytes(b"0" * 10)
    status, body = act(console, "cancel", id=RUST_ID)
    assert status == 200
    row = body["result"]["row"]
    assert row["state"] == "absent"
    assert "part" in row["note"]
    assert part.exists()  # the part stays: the next download resumes


def test_retry_from_failed(console):
    _queue(console, RUST_ID, RUST_FILE, acquire.FAILED,
           error={"code": "network", "message": "the download died", "retryable": True})
    status, body = act(console, "retry", id=RUST_ID)
    assert status == 200
    assert body["result"]["row"]["state"] == "queued"
    assert console.acquirer.attempts(RUST_ID) == 1
    # Retry is not for a row that is not failed.
    status2, body2 = act(console, "retry", id=RUST_ID)
    assert status2 == 409 and body2["error"]["code"] == "not_failed"


def test_remove_suggested_row_returns_to_absent(console):
    status, body = act(console, "remove", id=WIKI_ID)
    assert status == 200
    assert not (pathlib.Path(console.config.corpus) / (WIKI + ".zim")).exists()
    row = next(r for r in body["result"]["rows"] if r["id"] == WIKI_ID)
    assert row["state"] == "absent" and row["actions"] == ["download"]
    # The offer row is still in the list: removing a file is not removing the suggestion.


def test_remove_refused_without_a_file(console):
    status, body = act(console, "remove", id=RUST_ID)
    assert status == 409 and body["error"]["code"] == "nothing_on_disk"


# -- the verbs: the engine ----------------------------------------------------------------

def test_engine_start_launches_the_configured_set(console, fake):
    status, body = act(console, "engine_start")
    assert status == 200
    assert fake.launches == [sorted([WIKI, PY])]
    # The snapshot right after: the probe is still down (a fake launch starts nothing), so the
    # state reads `starting` inside the startup budget -- the page shows a spinner, not a lie.
    eng = body["result"]["engine"]
    assert eng["state"] == "starting"
    assert eng["served_books"] is None


def test_engine_start_without_an_exe_starts_the_fetch(console, tools):
    # The default config names no binary, and that is not an error: it is the fetch. The verb
    # answers 200 with the download in flight (the page shows progress), not a refusal.
    console.config.set("exe", None, source="test")
    status, body = act(console, "engine_start")
    assert status == 200 and body["result"]["engine"]["bin"]["state"] == "fetching"


def test_engine_stop_on_a_foreign_port_is_refused(console, fake):
    status, body = act(console, "engine_stop")
    assert status == 409 and body["error"]["code"] == "foreign_process"
    assert fake.stops == []


def test_engine_stop_uses_the_record(console, cfg):
    from siw import engine as engine_mod
    import time as _time
    engine_mod.write_pidfile(cfg, {"pid": os.getpid(), "port": 19191, "form": "paths",
                                   "started": _time.time()})
    try:
        status, body = act(console, "engine_stop")
        assert status == 200
        assert console.config  # the verb ran; the seam did the stopping
    finally:
        engine_mod.clear_pidfile(config_module.pidfile(cfg, 19191))


def test_engine_stop_sees_a_live_recorded_pid(console, cfg, fake):
    from siw import engine as engine_mod
    import time as _time
    engine_mod.write_pidfile(cfg, {"pid": os.getpid(), "port": 19191, "form": "paths",
                                   "started": _time.time()})
    try:
        eng = console.engine_snapshot()
        assert eng["started_by_siw"] is True
        assert eng["pid"] == os.getpid()
        assert eng["form"] == "paths"
        assert eng["started"] is not None
        assert eng["uptime_seconds"] is not None
    finally:
        engine_mod.clear_pidfile(config_module.pidfile(cfg, 19191))


# -- the verbs: settings --------------------------------------------------------------------

def test_set_settings_refuses_a_public_bind_without_credentials(console):
    status, body = act(console, "set_settings", bind="0.0.0.0")
    assert status == 400 and body["error"]["code"] == "credentials_required"


def test_set_settings_masks_the_password(console):
    status, body = act(console, "set_settings", bind="0.0.0.0", password="hunter2",
                       port=19193)
    assert status == 200
    pending = body["result"]["pending_apply"]
    by_field = {p["field"]: p for p in pending}
    assert set(by_field) == {"bind", "port", "password"}
    pw = by_field["password"]
    assert pw["to"] == "***" and pw["from"] == ""
    assert "hunter2" not in json.dumps(body)  # the real value never rides the wire
    assert all(set(p) == PENDING_KEYS for p in pending)
    # The real value is held, out-of-band, for the point of apply.
    assert console._secrets["password"] == "hunter2"


def test_set_settings_bad_port(console):
    status, body = act(console, "set_settings", port=99999)
    assert status == 400 and body["error"]["code"] == "bad_port"
    status2, body2 = act(console, "set_settings", port="twelve")
    assert status2 == 400 and body2["error"]["code"] == "bad_port"


def test_set_settings_refuses_a_taken_port(console, fake):
    fake.up = True  # the probe answers: something is on 19193
    status, body = act(console, "set_settings", port=19193)
    assert status == 409 and body["error"]["code"] == "bind_taken"
    fake.up = False


def test_config_apply_writes_the_file_and_keeps_the_secret(console, cfg, fake):
    act(console, "set_settings", password="hunter2")
    status, body = act(console, "config_apply")
    assert status == 200
    assert cfg.password == "hunter2"  # applied from the secret, not from the masked wire value
    assert console._secrets == {}
    assert body["result"]["pending_apply"] == []
    on_disk = json.loads(cfg.path.read_text(encoding="utf-8"))
    assert on_disk["password"] == "hunter2"
    assert fake.reexecs == ["reexec"]  # a server field applied: this process would relaunch


def test_config_discard_drops_the_pending(console):
    act(console, "set_settings", port=19195)
    status, body = act(console, "config_discard")
    assert status == 200 and body["result"]["pending_apply"] == []
    assert console.config.port != 19195  # discard is a no-op on the running config


# -- the verbs: scan and notices -------------------------------------------------------------

def test_scan_reports_the_added(console):
    """`added` is scan-to-scan against the console's own memory (n7): the first scan is
    the baseline, the second one names only what arrived in between."""
    status, body = act(console, "scan")
    assert status == 200 and body["result"]["added"] == []
    (pathlib.Path(console.config.corpus) / "new_book_en_2026-01.zim").write_bytes(b"3")
    status, body = act(console, "scan")
    assert status == 200
    assert body["result"]["added"] == ["new_book_en_2026-01"]
    assert body["result"]["corpus"]["books"] == 3


def test_dismiss_clears_a_notice(console):
    assert any(n["id"] == "engine-down" for n in console.state()["notices"])
    status, body = act(console, "dismiss", id="engine-down")
    assert status == 200
    assert all(n["id"] != "engine-down" for n in console.state()["notices"])


def test_done_notice_fires_once_and_is_dismissible(console):
    # A row that goes from downloading to on-disk earns a done-notice exactly once.
    _queue(console, RUST_ID, RUST_FILE, acquire.DOWNLOADING)
    console.state()  # poll it once: the queue state is remembered
    console.acquirer.forget(RUST_ID)
    (pathlib.Path(console.config.corpus) / RUST_FILE).write_bytes(b"4" * 100)
    console.scan(force=True)
    first = console.state()
    done = [n for n in first["notices"] if n["id"] == f"download-done:{RUST_ID}"]
    assert len(done) == 1 and done[0]["level"] == "ok"
    act(console, "dismiss", id=f"download-done:{RUST_ID}")
    assert not any(n["id"] == f"download-done:{RUST_ID}" for n in console.state()["notices"])


# -- preview and paste ------------------------------------------------------------------------

def test_preview_of_a_meta4_url(console, monkeypatch):
    calls = []
    monkeypatch.setattr(acquire, "fetch_meta4",
                        lambda url: calls.append(url) or {"size": 432000000,
                                                          "sha256": "ab" * 32})
    status, body = act(console, "preview",
                       url="https://lb.download.kiwix.org/z/wikipedia_en_all_mini_2026-05.zim.meta4")
    assert status == 200
    prev = body["result"]["preview"]
    assert set(prev) == PREVIEW_KEYS
    assert calls == ["https://lb.download.kiwix.org/z/wikipedia_en_all_mini_2026-05.zim.meta4"]
    assert prev["kind"] == "zim"
    assert prev["url"] == "https://lb.download.kiwix.org/z/wikipedia_en_all_mini_2026-05.zim"
    assert prev["size_bytes"] == 432000000
    assert prev["checksum_source"] == "meta4"
    assert prev["book"] == "wikipedia"  # named by the shipped scan, not the catalog
    assert prev["warning"] is None


def test_preview_of_a_catalog_page_follows_the_link(console, monkeypatch):
    page = ('<html><a href="https://lb.download.kiwix.org/z/devdocs_en_c_2026-02.zim">download</a>'
            '</html>')
    import siw.console as console_mod
    monkeypatch.setattr(console_mod, "_fetch_page", lambda url: page)
    monkeypatch.setattr(acquire, "fetch_meta4", lambda url: {"size": 1357824,
                                                             "sha256": "cd" * 32})
    status, body = act(console, "preview", url="https://browse.library.kiwix.org/some/entry")
    assert status == 200
    prev = body["result"]["preview"]
    assert prev["kind"] == "catalog_entry"
    assert prev["url"] == "https://lb.download.kiwix.org/z/devdocs_en_c_2026-02.zim"
    assert prev["sha256"] == "cd" * 32  # the sidecar the link points at was read
    assert prev["checksum_source"] == "meta4"
    assert prev["size_bytes"] == 1357824
    assert prev["warning"] is None


def test_preview_refuses_a_page_without_a_link(console, monkeypatch):
    import siw.console as console_mod
    monkeypatch.setattr(console_mod, "_fetch_page", lambda url: "<html>nothing here</html>")
    status, body = act(console, "preview", url="https://browse.library.kiwix.org/blank")
    assert status == 503 and body["error"]["code"] == "preview_unreachable"


def test_preview_refuses_a_non_http_paste(console):
    status, body = act(console, "preview", url="ftp://nope/x.zim")
    assert status == 400 and body["error"]["code"] == "unrecognized_url"


def test_download_url_requires_a_preview_first(console, monkeypatch):
    import siw.console as console_mod
    monkeypatch.setattr(acquire, "fetch_meta4", lambda url: {"size": 100, "sha256": "cd" * 32})
    monkeypatch.setattr(console_mod, "_fetch_page", lambda url: "<html></html>")
    url = "https://lb.download.kiwix.org/z/devdocs_en_c_2026-02.zim"
    status, body = act(console, "download_url", url=url)
    assert status == 409 and body["error"]["code"] == "preview_required"
    # After the preview, the same verb adds the row and queues it.
    status, body = act(console, "preview", url=url)
    assert status == 200
    status2, body2 = act(console, "download_url", url=url)
    assert status2 == 200
    row = body2["result"]["row"]
    assert row["state"] == "queued" and row["note"] == "pasted in through the last tier"
    # And the paste row persists for the life of the server.
    assert console._extra_path.exists()


def test_download_url_with_user_sha_skips_the_fetch(console, monkeypatch):
    import siw.console as console_mod
    def boom(url):
        raise AssertionError("a user-supplied sha must not fetch the sidecar")
    monkeypatch.setattr(acquire, "fetch_meta4", boom)
    monkeypatch.setattr(console_mod, "_fetch_page", boom)
    status, body = act(console, "download_url",
                       url="https://lb.download.kiwix.org/z/other_en_2026-01.zim",
                       sha256="ef" * 32)
    assert status == 200
    row = body["result"]["row"]
    assert row["state"] == "queued"
    assert row["size_bytes"] == 0  # no sidecar read: the size is unknown, the hash is not


# -- the pure functions ------------------------------------------------------------------------

def test_is_loopback():
    for addr in ("127.0.0.1", "localhost", "::1", "127.5.9.9"):
        assert is_loopback(addr), addr
    for addr in ("0.0.0.0", "::", "192.0.2.5", "", "*"):
        assert not is_loopback(addr), addr


def test_basic_auth():
    good = "Basic " + base64.b64encode(b"op:secret").decode()
    assert _basic_auth_ok(good, "op", "secret") is True
    assert _basic_auth_ok("Basic " + base64.b64encode(b"op:wrong").decode(), "op", "secret") is False
    assert _basic_auth_ok("Basic " + base64.b64encode(b"evil:secret").decode(), "op", "secret") is False
    assert _basic_auth_ok("Bearer junk", "op", "secret") is False
    assert _basic_auth_ok("Basic !!!not-base64!!!", "op", "secret") is False
    assert _basic_auth_ok("", "op", "secret") is False


def test_human():
    assert _human(1024**3) == "1.0 GB"
    assert _human(5 * 1024 * 1024) == "5 MB"


def test_offer_and_profiles_load(tmp_path):
    offer = load_offer()
    assert len(offer["books"]) == 9
    ids = [b["id"] for b in offer["books"]]
    assert "wikipedia/mini" in ids and "devdocs python" in ids
    assert all(b["url"].startswith("https://lb.download.kiwix.org/") for b in offer["books"])
    # The console list IS the default download set: every row is downloaded by download_all.
    # The 107 G stack overflow book is deliberately out of it (last tier / the agent asks).
    assert "stackoverflow" not in ids
    # The profile lookup is a user-supplied file, not repo content: the shipped tree
    # carries none, the index is empty, and identity falls back to the filename rung.
    assert load_profiles()["by_name"] == {} and load_profiles()["by_uuid"] == {}
    assert _match_profile("wikipedia_en_all_maxi_2026-08") is None
    assert _match_offered("devdocs_en_python_2026-10")["id"] == "devdocs python"
    assert _match_offered("no_such_book_2020-01") is None
    # A supplied file exercises both lookup keys (the server name, the embedded uuid).
    prof = tmp_path / "books.jsonl"
    prof.write_text(json.dumps({"name": "a book", "title": "A Book",
                                "serverName": "a_book_2026-01",
                                "uuid": "01234567-89ab-cdef-0123-456789abcdef",
                                "mediaCount": 0}) + "\n", encoding="utf-8")
    profiles = load_profiles(prof)
    assert profiles["by_name"]["a_book_2026-01"]["title"] == "A Book"
    assert profiles["by_uuid"]["01234567-89ab-cdef-0123-456789abcdef"]["media"] == "text"


def test_missing_offer_loads_empty(monkeypatch, tmp_path):
    import siw.console as console_mod
    monkeypatch.setattr(console_mod, "default_offer_path", lambda: tmp_path / "nope.json")
    offer = load_offer()
    assert offer["books"] == [] and offer["catalog_link"]


# -- the wire through a real socket ------------------------------------------------------------

def test_console_routes_on_a_real_socket(console, cfg):
    """The one test with a port: the page, /state, and /action as bytes on the wire, with the
    kernel's routes passing through beside them."""
    import siw.console as console_mod
    from http.server import ThreadingHTTPServer
    from siw import api as api_mod

    service = api_mod.Service(cfg)
    console_service = ConsoleService(console, cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console_mod.ConsoleHandler)
    server.daemon_threads = True
    server.service = service
    server.console_service = console_service
    server.console = console
    host, port = server.server_address
    console.set_bound(host, port)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def get(path):
            with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as reply:
                return reply.status, reply.read()

        status, page = get("/")
        assert status == 200 and b"Smol InterWebs" in page

        status, raw = get("/state")
        state = json.loads(raw)
        assert status == 200 and set(state) == STATE_KEYS

        # The kernel's routes pass through beside the console's.
        status, raw = get("/health")
        assert status == 200 and json.loads(raw)["service"] == "siw"

        # A verb over the wire, and the error shape over the wire.
        request = urllib.request.Request(
            f"http://{host}:{port}/action",
            data=json.dumps({"verb": "scan", "args": {}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as reply:
            body = json.loads(reply.read())
        assert body["ok"] is True and "added" in body["result"]

        request = urllib.request.Request(
            f"http://{host}:{port}/action",
            data=json.dumps({"verb": "warp", "args": {}}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read())
            assert exc.code == 404 and body["error"]["code"] == "unknown_verb"

        # Bad bodies are a 400 that names the shape, not a 500.
        request = urllib.request.Request(
            f"http://{host}:{port}/action", data=b"not json",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("expected a 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        server.shutdown()
        server.server_close()


# -- P1 regressions: the settings wiring (B1, B2), the boot refusal (B5), the CLI
# default verb and its flags (B6), the caller's sha reaching the queue (B4), and
# the done-notice only claiming verification the queue actually performed (B3).

def test_set_settings_saves_a_username_change_while_the_engine_runs(console, fake):
    """B1 [repro]: the old code seeded the new port from the *engine* port and probed
    it, so every save answered 409 bind_taken while a healthy engine ran. The seed is
    the API port, and nothing is probed unless bind or port actually changed."""
    fake.up = True  # something answers: our own healthy engine, on its own port
    status, body = act(console, "set_settings", username="someone", password="hunter2")
    assert status == 200, body
    assert {p["field"] for p in body["result"]["pending_apply"]} == {"username", "password"}
    # m4: a username arriving on its own is refused -- it authenticates nobody.
    status, body = act(console, "set_settings", username="nobody")
    assert status == 400 and body["error"]["code"] == "credentials_incomplete"


def test_set_settings_server_port_writes_api_port(console, cfg, fake):
    """B2 [repro]: the page's "Server port" filled from server.port -- the API port --
    but applying it wrote the *engine* port. It must write api_port and leave `port`."""
    status, _body = act(console, "set_settings", port=19193)
    assert status == 200
    status, _applied = act(console, "config_apply")
    assert status == 200
    assert cfg.api_port == 19193
    assert cfg.port == 19191  # the engine keeps its port
    on_disk = json.loads(cfg.path.read_text(encoding="utf-8"))
    assert on_disk["api_port"] == 19193 and on_disk["port"] == 19191


def test_serve_refuses_a_public_bind_without_a_password(tmp_path):
    """B5 [repro vs CONTRACT.md]: a non-loopback bind with no password is refused at
    boot -- the config file, SIW_BIND and --bind all land in the same check."""
    from siw.console import serve
    public = config_module.Config({"corpus": str(tmp_path), "bind": "0.0.0.0",
                                   "password": "", "api_port": 19299},
                                  path=tmp_path / "siw.json")
    assert serve(public) == 2


def test_cli_with_no_args_runs_the_default_verb(monkeypatch):
    """B6 [repro]: `python -m siw` unpacked a 1-element list and died with a
    ValueError traceback before the documented default verb could run."""
    import siw.console as console_mod
    import siw.__main__ as entry
    seen = []
    monkeypatch.setattr(console_mod, "serve", lambda *_a, **_k: seen.append(1) or 0)
    assert entry.main([]) == 0
    assert seen == [1]


def test_cli_bad_port_exits_cleanly():
    import siw.__main__ as entry
    with pytest.raises(SystemExit):
        entry.main(["serve", "--port", "abc"])


def test_download_url_sha_reaches_the_transfer(console, monkeypatch):
    """B4 [repro]: the sha was validated and then thrown away -- the transfer asked
    the sidecar and overwrote it. It must ride to the Transfer."""
    import siw.console as console_mod
    def boom(url):
        raise AssertionError("a user-supplied sha must not fetch anything")
    monkeypatch.setattr(acquire, "fetch_meta4", boom)
    monkeypatch.setattr(console_mod, "_fetch_page", boom)
    status, body = act(console, "download_url",
                       url="https://lb.download.kiwix.org/z/other2_en_2026-01.zim",
                       sha256="ab" * 32)
    assert status == 200, body
    assert console.acquirer._transfers["other2_en_2026-01"].sha256 == "ab" * 32


def test_done_notice_claims_only_the_verification_that_happened(console):
    """B3: a row that finished without a hash comparison says "Downloaded", not
    "Downloaded and verified". (The hash path is asserted in test_acquire.)"""
    _queue(console, RUST_ID, RUST_FILE, acquire.DOWNLOADING)
    console.state()
    console.acquirer.forget(RUST_ID)
    (pathlib.Path(console.config.corpus) / RUST_FILE).write_bytes(b"4" * 100)
    console.scan(force=True)
    done = [n for n in console.state()["notices"] if n["id"] == f"download-done:{RUST_ID}"]
    assert len(done) == 1
    assert "verified" not in done[0]["text"]


# -- the P2 battery: lifecycle, moves, the queue's folder, the origin rule ---------------

def test_the_starting_window_outlasts_the_launch_budget(console, fake):
    """The disabled-Start window was 15 s against a 60 s launch budget: half a minute
    before the server answered, the page re-enabled Start and a second launch raced
    the port. The window now names the same budget the launcher polls."""
    from siw import engine as engine_mod
    assert engine_mod.STARTUP_BUDGET > 15.0
    console._bring_up(raise_errors=False)
    assert console._engine_starting_until == fake.now + engine_mod.STARTUP_BUDGET
    assert fake.launches == [sorted([WIKI, PY])]


def test_two_engine_starts_never_race_the_port(console, monkeypatch):
    """The boot thread and an `engine_start` verb used to probe the same dark port and
    both launch; the loser of the port race idled orphaned. One lifecycle act at a time."""
    inside = {"n": 0, "max": 0}

    def slow_locked(raise_errors):
        inside["n"] += 1
        inside["max"] = max(inside["max"], inside["n"])
        _threading.Event().wait(0.05)
        inside["n"] -= 1
        return {"engine": {}}

    monkeypatch.setattr(console, "_bring_up_locked", slow_locked)
    threads = [_threading.Thread(target=console._bring_up, kwargs={"raise_errors": False})
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert inside["max"] == 1


def test_move_tasks_do_not_share_a_cancel_event():
    """`_cancel` was a class attribute: cancelling one move cancelled every move, and a
    move started after one was cancelled arrived already-cancelled."""
    from siw.console import MoveTask
    a = MoveTask(id="a", source="s", target="t")
    b = MoveTask(id="b", source="s", target="t")
    a._cancel.set()
    assert a._cancel is not b._cancel and not b._cancel.is_set()


def test_a_move_deletes_the_source_once_the_copy_agrees(console, cfg):
    """The move copied and renamed but never removed the originals: the disk held double,
    the engine saw both halves as duplicates, and a "move" that fills the next partition
    is a fault waiting for September. A size-agreeing copy takes the bytes off the source."""
    from siw.console import MoveTask
    src = pathlib.Path(cfg.corpus)
    names = sorted(p.name for p in src.iterdir())
    assert names
    target = src.parent / "moved"
    task = MoveTask(id="move-test", source=str(src), target=str(target),
                    bytes=sum(p.stat().st_size for p in src.iterdir()), started=1.0)
    console._run_move(task)
    assert task.state == "done" and task.error is None
    assert sorted(p.name for p in target.iterdir()) == names
    assert not any(src.glob("*.zim"))
    # And the queue follows the bytes: new downloads land in the folder that holds them.
    assert pathlib.Path(str(console.acquirer.corpus)) == target


def test_a_move_refuses_a_destination_that_already_holds_the_name(console, cfg):
    """A same-named file at the destination is not the same book — an updated snapshot
    keeps its name — and replace() would swallow it behind a size check alone. The move
    is refused before a byte moves; neither side is touched."""
    src = pathlib.Path(cfg.corpus)
    names = sorted(p.name for p in src.iterdir())
    assert names
    target = src.parent / "occupied"
    target.mkdir()
    victim = target / names[0]
    victim.write_bytes(b"a different book with the same name")
    status, body = act(console, "set_corpus_dir", path=str(target), move=True)
    assert status == 409 and body["error"]["code"] == "destination_collision"
    # The message names the clashing file AND the destination's file count: "holds 1 file"
    # once read as "the destination has one book", on a destination holding a hundred.
    message = body["error"]["message"]
    assert names[0] in message and "of its" in message
    assert victim.read_bytes() == b"a different book with the same name"
    assert sorted(p.name for p in src.iterdir()) == names
    assert not [t for t in console._tasks.values() if t.state in ("running", "queued")]


def test_the_boot_banner_is_one_fact_per_line():
    """The window opens with where the console answers, what it manages, and how to leave —
    each on its own line, the URLs aligned, nothing on a line that says two things."""
    from siw import config as config_module
    from siw.console import _boot_banner

    cfg = config_module.Config(values={"corpus": "/srv/zims"})
    lines = _boot_banner("0.1.0", "127.0.0.1", 7662, cfg).split("\n")
    assert lines[0] == "siw 0.1.0: console http://127.0.0.1:7662 (engine 127.0.0.1:18099, corpus /srv/zims)"
    assert lines[1] == "  spec:   http://127.0.0.1:7662/spec"
    assert lines[2] == "  health: http://127.0.0.1:7662/health"
    assert lines[3] == "  Ctrl-C stops it"


def test_the_console_window_shows_its_hits(console, cfg, monkeypatch, capsys):
    """A console window that prints nothing reads as dead: every hit gets its line (the
    kernel's API-quiet default does not fit a window people look at), and SIW_VERBOSE=0 keeps
    the silence for the run that wants it."""
    import siw.console as console_mod
    from http.server import ThreadingHTTPServer
    from siw import api as api_mod

    service = api_mod.Service(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console_mod.ConsoleHandler)
    server.daemon_threads = True
    server.service = service
    server.console_service = ConsoleService(console, cfg)
    server.console = console
    host, port = server.server_address
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The default run: the hit's line reaches the window (sys.stderr, resolved at write
        # time, so the runner's capture catches the request thread's write).
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5):
            pass
        _time.sleep(0.2)
        default_out = capsys.readouterr()
        assert '"GET /health HTTP/1.1" 200' in default_out.err

        # The quiet run: the same hit leaves no line.
        monkeypatch.setenv("SIW_VERBOSE", "0")
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5):
            pass
        _time.sleep(0.2)
        quiet_out = capsys.readouterr()
        assert "/health" not in quiet_out.err
    finally:
        server.shutdown()
        server.server_close()


def test_the_console_opens_only_on_a_plain_start(console):
    """The open decision is a pure predicate: the config says yes and this process is not the
    re-exec of an applied config (which must not steal a tab) → the console's own URL."""
    from siw.console import _open_console_url
    assert _open_console_url(console.config) == "http://127.0.0.1:19192/"
    console.config.set("open_browser", False)
    assert _open_console_url(console.config) is None
    console.config.set("open_browser", True)
    assert _open_console_url(console.config, {"SIW_REEXEC": "1"}) is None


def test_media_is_read_from_the_engine_catalog(console, fake):
    """The media column is fed, not guessed: a served book's content comes from the engine's
    own catalog (mediaCount) and outranks the offer's stated value; a book the engine does
    not serve keeps its prior, and unknown is the floor."""
    fake.entries = [
        {"id": "u1", "name": "Wikipedia", "title": "Wikipedia", "language": "en",
         "article_count": "123", "content_name": WIKI, "media_count": 0},
        {"id": "u2", "name": "DevDocs", "title": "DevDocs", "language": "en",
         "article_count": "456", "content_name": PY, "media_count": 5},
    ]
    fake.up = True
    (pathlib.Path(console.config.corpus) / "extras.zim").write_bytes(b"0" * 32)
    state = console.state()
    assert row_by_id(state, WIKI_ID)["media"] == "text"            # catalog: 0 media
    assert row_by_id(state, PY_ID)["media"] == "text+images"       # catalog: 5, outranks the offer
    assert row_by_id(state, "extras")["media"] == "unknown"        # unserved, no prior


def test_a_landed_offer_download_cannot_regress_to_absent(console, cfg):
    """A finished download drops its transfer from the queue; a scan cached before the landing
    knows nothing of the file, and the row fell back to absent — a fresh Download button at
    100%, inviting a double-start. A book that has landed is on disk; the row must say so."""
    act(console, "scan")  # a fresh cache, taken while rust is still absent
    (pathlib.Path(cfg.corpus) / RUST_FILE).write_bytes(b"0" * 64)
    console.acquirer._promoted_seq += 1  # the success path's signal
    row = row_by_id(console.state(), RUST_ID)
    assert row["state"] in ("downloaded", "served")
    assert "download" not in row["actions"]


def test_a_scanned_book_beyond_the_offer_earns_the_same_link(console, fake):
    """The page link is the engine's, not the offer's: a book the console scanned in — not in
    the predefined set — opens exactly as an offered one does, once the engine is up."""
    (pathlib.Path(console.config.corpus) / "some_scanned_book.zim").write_bytes(b"0" * 64)
    fake.up = True
    row = row_by_id(console.state(), "some_scanned_book")
    assert row is not None
    assert row["served"]["page_url"] == "http://127.0.0.1:19191/content/some_scanned_book/"


def test_set_corpus_dir_repoints_the_queue(console, tmp_path):
    """The scan moved to the new folder and `queue()` kept writing parts into the old
    one, where nothing scanned them: two folders, one corpus, no error anywhere."""
    console._runtime_corpus = str(tmp_path / "empty")  # the re-point branch: nothing to move
    other = tmp_path / "elsewhere"
    other.mkdir()
    status, _body = act(console, "set_corpus_dir", path=str(other))
    assert status == 200
    assert pathlib.Path(str(console.acquirer.corpus)) == other


def test_set_corpus_refuses_under_a_live_transfer(acq, tmp_path):
    acq.queue("r", "https://x.example/r.zim", "r.zim")
    acq._transfers["r"].state = acquire.DOWNLOADING
    with pytest.raises(acquire.AcquireError) as got:
        acq.set_corpus(str(tmp_path / "elsewhere"))
    assert got.value.code == "transfer_active"


def test_stop_kills_the_child_still_writing(acq):
    """`siw serve` could exit with its curl alive and still appending to a part; stop()
    now settles the queue it owns."""
    class FakeChild:
        def __init__(self):
            self.dead = False

        def terminate(self):
            self.dead = True

        def wait(self, timeout=None):
            return 0

    acq.queue("r", "https://x.example/r.zim", "r.zim")
    child = FakeChild()
    acq._transfers["r"].state = acquire.DOWNLOADING
    acq._transfers["r"].child = child
    acq.stop()
    assert child.dead


def test_a_queue_refusal_reaches_the_wire_as_a_refusal(console, monkeypatch):
    """The queue names its refusals (`no_corpus`, `transfer_active`); leaving the console's
    crash handler answer them turned a rule into a 500 and a traceback."""
    def boom(*_a, **_k):
        raise acquire.AcquireError("no_corpus", "there is no corpus folder to write into")

    monkeypatch.setattr(console.acquirer, "queue", boom)
    status, body = act(console, "download_url", url="https://x.example/x.zim",
                       sha256="a" * 64)
    assert status == 409 and body["error"]["code"] == "no_corpus"


def test_a_failed_preview_names_the_class_not_the_failure_details(console, monkeypatch):
    """The failure text is an oracle into the box: local paths, proxy internals, whatever
    the refusing library chose to say. The class answers; the text stays."""
    import siw.console as console_mod

    def boom(url):
        raise RuntimeError("open failed for C:\\Users\\example\\.secret\\token at proxy:7")

    monkeypatch.setattr(console_mod, "resolve_preview", boom)
    status, body = act(console, "preview", url="https://x.example/x.meta4")
    assert status == 503
    assert "Users" not in body["error"]["message"]
    assert "RuntimeError" in body["error"]["message"]


def test_the_preview_fetch_is_bounded():
    """The preview fetches any URL the operator pastes. Its bytes, redirects and time
    are bounded; the capability itself is documented, not this constant."""
    import siw.console as console_mod
    assert console_mod.PAGE_CAP == 1024 * 1024
    assert console_mod.PAGE_TIMEOUT == 30.0
    assert console_mod._CappedRedirectHandler.max_redirections == 3


def test_the_origin_rule_over_a_real_socket(console, cfg):
    """Cross-origin writes are refused before the verb runs; same-origin writes, plain
    non-browser clients, and the kernel's own reads are unaffected -- and the kernel no
    longer ships a blanket `Access-Control-Allow-Origin: *` inviting the cross-origin
    read it then has to refuse to write."""
    import siw.console as console_mod
    from http.server import ThreadingHTTPServer
    from siw import api as api_mod

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_mod.ConsoleHandler)
    server.daemon_threads = True
    server.service = api_mod.Service(cfg)
    server.console_service = ConsoleService(console, cfg)
    server.console = console
    host, port = server.server_address
    console.set_bound(host, port)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def post(headers):
            request = urllib.request.Request(
                f"http://{host}:{port}/action",
                data=json.dumps({"verb": "scan", "args": {}}).encode(),
                headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(request, timeout=5) as reply:
                    return reply.status, json.loads(reply.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        status, body = post({"Origin": "http://evil.example"})
        assert status == 403 and body["error"]["code"] == "cross_origin"
        status, _body = post({"Sec-Fetch-Site": "cross-site"})
        assert status == 403
        status, body = post({"Origin": f"http://{host}:{port}", "X-Requested-With": "siw"})
        assert status == 200 and body["ok"] is True
        status, _body = post({})  # curl, adapters: no Origin to lie about, unconstrained
        assert status == 200

        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as reply:
            assert reply.headers.get("Access-Control-Allow-Origin") is None
    finally:
        server.shutdown()
        server.server_close()


def test_the_target_name_survives_query_strings_and_banned_characters():
    """m10: the download target used to be the raw URL basename, so `.../a.zim?x=1`
    became a file name Windows refuses and the failure surfaced from inside curl."""
    from siw.console import _target_name
    assert _target_name("https://x.example/a.zim?x=1") == "a.zim"
    assert _target_name("https://x.example/we%3Cir>d.zim") == "weird.zim"
    assert _target_name("https://x.example/a.zim.") == "a.zim"
    assert _target_name("https://x.example/%2e%2e/trail") == "trail"
    assert _target_name("https://x.example//") == "download.zim"


def test_the_pasted_rows_file_cannot_smuggle_a_non_http_target(console):
    """m8: siw-pasted.json is user state on disk; a tampered row used to become a
    fetchable URL on the next boot. Only http(s) rows survive the load."""
    console._extra_path.write_text(
        '[{"id": "evil", "url": "file:///C:/Users/example/vuln.txt"},'
        ' {"id": "ok", "url": "https://x.example/ok.zim"}]', encoding="utf-8")
    console._extra_rows = []
    console._load_extra()
    assert [r["id"] for r in console._extra_rows] == ["ok"]


def test_finished_move_tasks_are_pruned(console, cfg):
    """m8: every move ever run used to ride every /state snapshot forever. A new move's
    start keeps only the most recent finished ones."""
    from siw.console import MoveTask
    for i in range(30):
        t = MoveTask(id=f"move-old-{i}", source="s", target="t")
        t.state = "done"
        console._tasks[t.id] = t
    target = pathlib.Path(cfg.corpus).parent / "pruned-target"
    task = console._start_move(str(cfg.corpus), str(target))
    task._cancel.set()  # the copy is beside the point; this is about the bookkeeping
    assert len(console._tasks) <= 21  # TASK_KEEP finished + the one just started
