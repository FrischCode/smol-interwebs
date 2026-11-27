"""siw.api -- the HTTP surface: routes in, JSON out, and the spec served by the running server.

One handler class, five routes, and one answer shape: a JSON object, always, whether it worked or not.
A failure is `{"error": {"code": ..., "message": ...}}` with the status that names the kind of failure,
so an adapter has one error path and a machine has a status code to branch on. Nothing here is HTML and
nothing here is a template — the intended reader is a program.

**The engine's address is not a parameter.** A request can name a query, a limit, a source filter and a
corpus filter; it cannot name a host, a port, a directory or a binary. Those are the operator's act at
the config: the model picks *among* the operator's
corpus and never directs the tool to a place the operator did not choose. Loopback by bind, no auth — an
unauthenticated server on a reachable interface is a share of the machine, and neither kiwix-serve nor
this surface has the machinery to make that safe.

**The spec is served, not shipped.** `/spec` answers the OpenAPI document, so an agent points at a running
server and reads what *that* server does instead of reading a checkout that may describe another version
of it. The document is derived from the routes below, and the file it reads is the repo's `docs/api.json`, overridable with
`SIW_SPEC` for a deployment that stages it elsewhere.

Threading is the design, not a tuning knob: a first query against a cold corpus can take seconds per book,
and a server that answered one request at a time would block a health check behind someone else's search.
Every connection is its own daemon thread, so a stopped server stops.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from siw import __version__, config as config_module
from siw import corpus, engine, search

# The routes, and the parameters each accepts. A parameter outside this set is a 400 that names what it
# did accept: an adapter guessing a spelling gets an answer better than a silently ignored argument.
ROUTE_PARAMS = {
    "/health": (),
    "/search": ("q", "query", "limit", "sources", "corpus", "heal", "mode", "books"),
    "/catalog": ("q", "picked", "served"),
    "/spec": (),
    "/": (),
}

# The five status codes this surface answers, and what each one means to the caller. Anything else is a
# bug, and the 500 says so with the exception's own words.
ERRORS = {
    "400": "the request is not callable: a missing query, an unknown parameter, a value out of range",
    "404": "no such route (the route list is at `/`)",
    "405": "the method is not GET",
    "409": "the request is well-formed but the server cannot honour it as it stands (a book it does not "
           "have, since the last time the engine loaded)",
    "500": "the route raised: a bug in this server, not in the request",
    "502": "the engine answered, and its answer was not a search result",
    "503": "the engine is not answering and could not be started",
}


class Service:
    """One server's state: the config it was started on, and the facts it reads per request.

    The config is read once at start and held: a running server that re-read its config file per request
    would change the corpus under a client mid-session, and the served set would silently stop matching
    what the process launched."""

    def __init__(self, config_, spec_path: str | Path | None = None):
        self.config = config_
        self.spec_path = Path(spec_path) if spec_path else default_spec_path()
        self.started = time.time()

    # -- routes -------------------------------------------------------------

    def index(self) -> dict:
        """`GET /` — the card a curious client reads first: what this is, and every route it answers."""
        return {"service": "siw", "name": "Smol InterWebs", "version": __version__,
                "purpose": ("offline fulltext search over the corpus this machine configured, "
                            "served as JSON"),
                "routes": {
                    "GET /health": "engine state, endpoint, bind, served vs configured, drift",
                    "GET /search?q=&limit=&sources=&corpus=&heal=&mode=&books=": "the search record and its results; mode=grouped asks each book its own question and answers with the best per-book",
                    "GET /catalog?q=&picked=&served=": "the books, their sizes, the pick, what is served",
                    "GET /spec": "this server's OpenAPI document, as JSON",
                    "POST /mcp": "the Model Context Protocol route: the search, list_books and add_book tools for an agent (JSON-RPC 2.0)",
                    "GET /": "this card"},
                "config": str(self.config.path) if self.config.path else None,
                "errors": ERRORS}

    def health(self) -> dict:
        """`GET /health` — the deployment's vital signs, read live: is the engine answering, on what
        address is it actually bound, how many books does it serve against how many the config picked."""
        cfg = self.config
        books = corpus.list_books(cfg.corpus)
        picked = corpus.select(books, cfg.selected_books())
        up = engine.probe(cfg.host, cfg.port)
        entries, problem = (engine.catalog(cfg, timeout=10.0) if up else ([], "engine down"))
        served = None if problem else len(entries)
        _bound = engine.listening_on(int(cfg.port))  # one netstat pass, not two
        drift = corpus.diff([b.name for b in picked], [e["content_name"] for e in entries]) \
            if served is not None else None
        record, _where = engine.read_pidfile(cfg)
        return {"ok": bool(up), "service": "siw", "version": __version__,
                "engine": {"host": cfg.host, "port": int(cfg.port), "up": bool(up),
                           "bound_on": _bound,
                           "loopback_only": loopback_only(_bound),
                           "form": (record or {}).get("form"),
                           "started_by_siw": bool(record),
                           "catalog_problem": problem or None},
                "corpus": {"directory": cfg.corpus, "books": len(books), "picked": len(picked),
                           "parts": corpus.parts(cfg.corpus), "bytes": sum(b.size for b in picked)},
                "served": served, "drift": drift,
                "api": {"host": cfg.bind, "port": int(cfg.api_port),
                        "uptime_seconds": round(time.time() - self.started, 1)},
                "config_path": str(cfg.path) if cfg.path else None,
                "platform": config_module.platform_note(),
                "spawn_proven": engine.platform_support()[0]}

    def do_search(self, params: dict) -> dict:
        """`GET /search` — the answer record. `q` is required; `heal=0` refuses to start an engine rather
        than launching one as a side effect of a query."""
        words = (params.get("q") or params.get("query") or "").strip()
        if not words:
            raise search.SearchError("missing_query",
                                     "a search needs q: /search?q=<words>", 400)
        heal = (params.get("heal", "1") or "1").strip().lower() not in ("0", "false", "no")
        mode = (params.get("mode", "global") or "global").strip().lower()
        if mode not in ("global", "grouped"):
            raise search.SearchError("bad_mode", f"mode wants global or grouped, got {mode!r}", 400)
        limit = params.get("limit", "10" if mode == "grouped" else "5")
        try:
            limit_n = int(limit)
        except (TypeError, ValueError):
            raise search.SearchError("bad_limit", f"limit wants an integer, got {limit!r}", 400) from None
        # A named universe is what makes a grouped question fair: the caller's own list, every one of
        # its books answered. Un-named, the universe is the configured scope and the answer is the
        # topic map — so the scope list is only resolved for the routes that take it.
        books = None if mode == "grouped" else corpus.select(corpus.list_books(self.config.corpus),
                                                             self.config.selected_books())
        if mode == "grouped":
            wanted = [w.strip() for w in re.split(r"[\s,;]+", params.get("books", "")) if w.strip()]
            if wanted:
                scope_books = corpus.select(corpus.list_books(self.config.corpus),
                                            self.config.selected_books())
                narrowed = corpus.select(scope_books, wanted)
                known = {b.name.lower() for b in scope_books} | {b.short_name.lower() for b in scope_books}
                unknown = [w for w in wanted if w.lower() not in known]
                if unknown:
                    raise search.SearchError("unknown_book",
                                             f"books= named no book in scope: {', '.join(unknown)} — the "
                                             "names are the file stems; `GET /catalog` lists them", 400)
                books = narrowed
        return search.search(self.config, words, limit=limit_n, sources=params.get("sources", ""),
                             corpus_filter=params.get("corpus", ""), books=books, heal=heal, mode=mode)

    def catalog(self, params: dict) -> dict:
        """`GET /catalog` — the books this machine has, which of them the pick holds, and (with
        `served=1`) what the engine says it serves. The sizes are why a user can pick sensibly."""
        cfg = self.config
        books = corpus.list_books(cfg.corpus)
        picked_names = cfg.selected_books()
        picked = corpus.select(books, picked_names)
        term = params.get("q", "")
        shown = corpus.matching(books, term) if term else books
        if (params.get("picked", "") or "").lower() in ("1", "true", "yes"):
            # `picked=1` is the "show me what I already chose" view the catalog route and an advisor both want,
            # and a parameter that is accepted and ignored is the failure mode this API promises against.
            chosen = {b.name for b in picked}
            shown = [b for b in shown if b.name in chosen]
        served = None
        if (params.get("served", "") or "").lower() in ("1", "true", "yes"):
            entries, _problem = engine.catalog(cfg, timeout=15.0)
            served = [e["content_name"] for e in entries]
        return {"corpus": cfg.corpus, "count": len(books), "picked": len(picked),
                "whole_corpus": picked_names is None,
                "filter": term or None, "shown": len(shown),
                "cost": corpus.cost_report(picked),
                "unknown_picks": corpus.unknown_names(books, picked_names),
                "books": [{"name": b.name, "short_name": b.short_name, "size_bytes": b.size,
                           "picked": b in picked} for b in shown],
                "served": served}

    def spec(self) -> dict:
        """`GET /spec` — the machine-readable contract, read from the file this server was built with.

        A missing spec is a 500 with the path it looked at, never an empty object: an agent that reads a
        silent spec would write an adapter against nothing."""
        if not self.spec_path.is_file():
            raise search.SearchError("spec_missing", f"no OpenAPI document at {self.spec_path}", 500)
        try:
            return json.loads(self.spec_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise search.SearchError("spec_unreadable", f"{self.spec_path} is not valid JSON: {exc}",
                                     500) from exc

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, method: str, target: str) -> tuple[int, dict]:
        """The route table, as a pure function: method and target in, status and payload out.

        The HTTP layer below is five lines over this, so the whole contract is testable without a socket,
        and the shapes the spec declares are the shapes these return."""
        parsed = urlparse(target)
        path = unquote(parsed.path).rstrip("/") or "/"
        if method.upper() != "GET":
            return 405, _error("not_get", f"{method} is not supported; every route is a GET")
        allowed = ROUTE_PARAMS.get(path)
        if allowed is None:
            return 404, _error("no_route", f"no route {path}; see GET / for the route list")
        query = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        unknown = [k for k in query if k not in allowed]
        if unknown:
            return 400, _error("unknown_parameter",
                               f"{', '.join(sorted(unknown))} is not a parameter of {path}; "
                               f"this route takes: {', '.join(allowed) or 'nothing'}")
        try:
            if path == "/":
                return 200, self.index()
            if path == "/health":
                return 200, self.health()
            if path == "/search":
                return 200, self.do_search(query)
            if path == "/catalog":
                return 200, self.catalog(query)
            return 200, self.spec()
        except search.SearchError as exc:
            return exc.status, _error(exc.code, str(exc))
        except (engine.EngineError, config_module.ConfigError) as exc:
            return 503, _error("engine_error", str(exc))
        except OSError as exc:
            return 503, _error("io_error", f"{exc.__class__.__name__}: {exc}")
        except Exception as exc:  # a bug in this server, said as JSON rather than as a dropped socket
            return 500, _error("crash", f"{exc.__class__.__name__}: {exc}")


def _error(code: str, message: str) -> dict:
    """One error shape everywhere: a code a machine can branch on, a sentence a person can act on."""
    return {"error": {"code": code, "message": message}}


def default_spec_path() -> Path:
    """Where the spec lives for this checkout: `docs/api.json` beside the package, or `$SIW_SPEC`.

    Read from the tree rather than embedded in the code so the document that describes the server is the
    file a reviewer can diff, and a deployment that installed the package without its docs can point the
    server at a staged copy instead of losing `/spec`."""
    if os.environ.get("SIW_SPEC"):
        return Path(os.environ["SIW_SPEC"]).expanduser()
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "docs" / "api.json", here / "docs" / "api.json"):
        if candidate.is_file():
            return candidate
    return here.parent / "docs" / "api.json"


class Handler(BaseHTTPRequestHandler):
    """The five lines of HTTP over `Service.dispatch`: read the request, hand it over, send the answer.

    `protocol_version` stays HTTP/1.0-by-default off and set to 1.1 with a deliberate `Connection: close`,
    because a keep-alive socket held by a client that never reads again is a thread that never ends; and
    the server header names this tool and nothing else, so a port scan sees what is actually listening."""

    server_version = f"siw/{__version__}"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def do_PUT(self) -> None:
        self._answer("PUT")

    def _answer(self, method: str) -> None:
        service: Service = self.server.service  # type: ignore[attr-defined]
        try:
            status, payload = service.dispatch(method, self.path)
        except Exception as exc:  # a route that raises must not take the connection or the thread down
            status, payload = 500, _error("crash", f"{exc.__class__.__name__}: {exc}")
        body = (json.dumps(payload, indent=None, default=str) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # No CORS header, deliberately: a wildcard default would let any page read this
        # box; readers reach it same-origin or with the password.
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client hung up; there is nobody left to tell

    def log_message(self, *args) -> None:
        """Stdlib request logging, off by default: an API polled by an advisor would drown the operator's
        terminal. `SIW_VERBOSE=1` turns the lines back on for a debugging run."""
        if os.environ.get("SIW_VERBOSE"):
            super().log_message(*args)


def make_server(service: Service, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    """The bound socket. Loopback by default, `SO_REUSEADDR` on, and a daemon-threaded pool.

    This kernel server carries no credential layer of its own — the console (`siw.serve`, the product's
    verb) is the surface that adds them. So the kernel refuses a bind that is not loopback outright:
    a corpus served to the network with no password is the one thing this tool promises not to do, and
    a public function may not be the way it happens by accident."""
    cfg = service.config
    address = (host or cfg.bind, int(port or cfg.api_port))
    if not is_loopback(address[0]):
        raise ValueError(f"the bare kernel server serves without credentials; {address[0]} is beyond "
                         "loopback — run `siw serve` (the console, which enforces the password rule)")
    httpd = ThreadingHTTPServer(address, Handler)
    httpd.daemon_threads = True
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def serve(config_, host: str | None = None, port: int | None = None,
          on_ready=None) -> None:
    """The human door: bind, announce, and answer until Ctrl-C.

    The announcement is printed before the first request because the operator needs to see the endpoint and
    the bind it is actually on, in the same lines that say whether the engine under it came up: a server
    that starts quietly and serves nothing is the failure this verb exists to prevent."""
    service = Service(config_)
    httpd = make_server(service, host, port)
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    print(f"siw {__version__}: http://{bound_host}:{bound_port} "
          f"(engine {config_.host}:{config_.port}, corpus {config_.corpus})")
    print(f"  spec: http://{bound_host}:{bound_port}/spec   health: "
          f"http://{bound_host}:{bound_port}/health   Ctrl-C stops it")
    if on_ready:
        on_ready(service, httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nsiw: stopped")
    finally:
        httpd.server_close()


LOOPBACK_NAMES = {"127.0.0.1", "::1", "localhost"}


def is_loopback(bind: str) -> bool:
    """Whether an address this server would bind is loopback-only. `0.0.0.0` and `::` are the
    spellings of every interface; a bare `127.` or `::1` is the loopback the credential rule
    turns off on. This is the one predicate of its kind; `siw.console.is_loopback` is this
    function, re-exported."""
    b = (bind or "").strip().lower()
    if b in ("", "0.0.0.0", "::", "*"):
        return False
    if b in LOOPBACK_NAMES:
        return True
    if b.startswith("127."):
        return True
    if b.startswith("::1"):
        return True
    return False


def loopback_only(bound_on: list[str]) -> bool | None:
    """Is every address this port answers on a loopback one? None when the listing could not say.

    `0.0.0.0` and `::` are the two spellings of "every interface I have"; a machine that answers on them
    with no auth is serving its corpus to the network, and that is worth a boolean the doctor can print
    without a human reading a netstat block. An entry the listing spells with a leading `<` (a state
    rather than an address) means the same thing: cannot say."""
    if not bound_on:
        return None
    if any(str(where).startswith("<") for where in bound_on):
        return None
    return all(is_loopback(str(where)) for where in bound_on)


_loopback_only = loopback_only


def health_thread(service: Service, every: float = 30.0) -> threading.Thread:
    """A daemon that keeps nothing: the hook for a deployment that wants a periodic health line in a log.

    Present because a detached server with no output is indistinguishable from a wedged one, and the
    operator's remedy should be a log line rather than a guess. Off unless started — a server that logs on
    a timer is a server that writes when nobody asked."""
    def loop() -> None:
        while True:
            time.sleep(every)
            try:
                report = service.health()
                print(f"[siw] {report['engine']['up'] and 'engine up' or 'engine DOWN'} "
                      f"served={report['served']} picked={report['corpus']['picked']}")
            except Exception as exc:  # a health line must never be the thing that kills the server
                print(f"[siw] health check failed: {exc.__class__.__name__}: {exc}")

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
