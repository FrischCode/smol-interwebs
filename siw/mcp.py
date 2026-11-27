"""siw.mcp -- the Model Context Protocol route: the agent's front door to this machine's corpus.

One POST endpoint (`/mcp`), JSON-RPC 2.0 in and out, stateless: no session ids, no SSE — a single
JSON-RPC message in, a single JSON object out (a batch array in, an array out). Three tools, and
every one of them is a thin adapter over the console service and the kernel's search — no retrieval
logic lives here, and the route answers behind the same loopback/credential policy as the rest of
the surface (a bound interface means a bound MCP route; the protocol adds no auth of its own).

The `search` tool's description is rebuilt on every `tools/list` so it carries the current roster.
"""
from __future__ import annotations

import json
import re

from siw import __version__, corpus
from siw.search import SearchError, search as run_search

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "smol-interwebs"
TOOL_NAMES = ("search", "list_books", "add_book")


class _ToolError(Exception):
    """A tool execution failure: the JSON-RPC call itself went through, the result says what went wrong."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _ok(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _size(size) -> str:
    """A byte count rounded to the unit a reader can place."""
    try:
        b = int(size)
    except (TypeError, ValueError):
        return "?"
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= div:
            v = b / div
            return f"{v:.0f} {unit}" if v >= 100 else f"{v:.1f} {unit}"
    return f"{b} B"


def _error_message(body: dict, status: int) -> str:
    error = (body or {}).get("error") or {}
    return f"HTTP {status}: {error.get('code', 'error')}: {error.get('message', 'the server refused')}"


def _row_brief(row: dict) -> dict:
    return {"id": row.get("id"), "book": row.get("book"), "title": row.get("title"),
            "size_bytes": row.get("size_bytes"), "state": row.get("state"), "url": row.get("url")}


# -- the tools ----------------------------------------------------------------

def _roster(cs) -> tuple[str, int, str]:
    """The live served set, for the search description: who is queryable right now."""
    state = cs.state()
    rows = [r for r in (state.get("rows") or []) if r.get("state") == "served"]
    eng_state = (state.get("engine") or {}).get("state") or "unknown"
    if not rows:
        roster = "no books served yet (the engine is not up, or nothing has been downloaded)"
    else:
        parts = [f"{r.get('book')} ({_size(r.get('size_bytes'))})" for r in rows[:20]]
        roster = ", ".join(parts) + (f", +{len(rows) - 20} more" if len(rows) > 20 else "")
    return roster, len(rows), eng_state


def tools(cs) -> list[dict]:
    """The tool list, with the search description rebuilt around the roster as it stands right now."""
    roster, n, eng_state = _roster(cs)
    return [
        {"name": "search",
         "description": (f"Offline fulltext search over this machine's ZIM corpus. Served now "
                         f"({n} books, engine {eng_state}): {roster}. "
                         "mode=global (default) is the engine's own merged ranking over everything "
                         "served — one request, fast, but a big index (a multi-million-article "
                         "wikipedia) dominates the top. mode=grouped asks each book its own question "
                         "and it answers with its best hit plus its hit count, ordered by hit count — "
                         "use it when you want the best few ACROSS the books rather than the best few "
                         "of one book; with books= named, every named book that has a hit answers with "
                         "its best hit (the caller chose the universe, so nothing in it is sliced); "
                         "un-named, the answer is the top-`limit` books by hit count — the topic map "
                         "(list_books shows the names). Search is AND across all terms; a zero-hit answer carries a "
                         "zero_hit field that says what the zero does not mean."),
         "inputSchema": {"type": "object",
                         "properties": {
                             "query": {"type": "string",
                                       "description": "The words to search for (AND across all terms)."},
                             "mode": {"type": "string", "enum": ["global", "grouped"],
                                      "description": "global (default): one merged ranking over everything served. "
                                                     "grouped: per-book answers, best hit plus hit count each, "
                                                     "ordered by hit count."},
                             "books": {"type": "array", "items": {"type": "string"},
                                       "description": "mode=grouped only: the book names (file stems) to ask — "
                                                      "every named book that has a hit is answered. Absent = the "
                                                      "whole scope, answered as the top-`limit` books by hit count."},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 25,
                                       "description": "Max results. Default 5 (global) / 10 (grouped)."}},
                         "required": ["query"]}},
        {"name": "list_books",
         "description": ("The books this machine holds and serves: for each, the book name (the "
                         "`books=` argument of search), title, media type, size, state "
                         "(served / downloaded / absent / queued / downloading / failed) and, for "
                         "served books, the page URL. Use it to choose the books= universe of a "
                         "grouped search."),
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "add_book",
         "description": ("Add a book to the corpus from a .zim URL (the \"Direct\" download link "
                         "from the Kiwix Catalog's download dialog). First call returns the "
                         "preview — book name, size, sha256 — without fetching anything. Call "
                         "again with confirm:true to start the download, or supply the sha256 on "
                         "the first call to skip the round-trip. The transfer verifies the "
                         "sha256 when one is given, and the ZIM header otherwise, before the "
                         "book counts as added. The fetch reaches any http(s) URL you give it, "
                         "including hosts on your own network."),
         "inputSchema": {"type": "object",
                         "properties": {
                             "url": {"type": "string",
                                     "description": "The .zim download URL (http/https)."},
                             "confirm": {"type": "boolean",
                                         "description": "Start the download now (after the preview)."},
                             "sha256": {"type": "string",
                                        "description": "64 hex digits; satisfies the preview gate on the first call."}},
                         "required": ["url"]}},
    ]


def _tool_search(cs, arguments: dict) -> dict:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise _ToolError("query is required: the words to search for")
    mode = str(arguments.get("mode") or "global").strip().lower()
    if mode not in ("global", "grouped"):
        raise _ToolError(f"mode wants global or grouped, got {mode!r}")
    limit = arguments.get("limit")
    try:
        limit_n = int(limit) if limit is not None else (10 if mode == "grouped" else 5)
    except (TypeError, ValueError):
        raise _ToolError(f"limit wants an integer, got {limit!r}")
    cfg = cs.config
    scope_books = corpus.select(corpus.list_books(cfg.corpus), cfg.selected_books())
    wanted = [str(w).strip() for w in (arguments.get("books") or []) if str(w).strip()]
    books = scope_books
    if wanted:
        if mode != "grouped":
            raise _ToolError("books= only makes sense with mode=grouped: the global route is the "
                             "engine's own merged ranking over everything served")
        known = {b.name.lower() for b in scope_books} | {b.short_name.lower() for b in scope_books}
        unknown = [w for w in wanted if w.lower() not in known]
        if unknown:
            raise _ToolError(f"books named no book in scope: {', '.join(unknown)} — list_books shows the names")
        books = corpus.select(scope_books, wanted)
    if mode == "grouped" and not wanted:
        books = None  # the un-named shape: the kernel takes the configured scope and the topic-map limit
    try:
        record = run_search(cfg, query, limit=limit_n, books=books, heal=True, mode=mode)
    except SearchError as exc:
        raise _ToolError(f"{exc.code}: {exc}")
    return record


def _tool_list_books(cs) -> dict:
    state = cs.state()
    rows = state.get("rows") or []
    books = [{"name": r.get("id"), "book": r.get("book"), "title": r.get("title"),
              "media": r.get("media"), "size_bytes": r.get("size_bytes"), "state": r.get("state"),
              "page_url": (r.get("served") or {}).get("page_url")} for r in rows]
    engine = state.get("engine") or {}
    return {"engine": {"state": engine.get("state"), "url": engine.get("url")},
            "served": sum(1 for b in books if b["state"] == "served"),
            "books": books}


def _tool_add_book(cs, arguments: dict) -> dict:
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise _ToolError("url is required: the .zim download URL")
    sha = str(arguments.get("sha256") or "").strip().lower() or None
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise _ToolError("sha256 wants 64 hex digits")
    if sha:
        # The checksum came with the request: nothing is fetched before it is checked, so the
        # preview gate is satisfied by the sha and the download starts on the first call.
        status, body = cs.action("download_url", {"url": url, "sha256": sha})
        if status != 200:
            raise _ToolError(_error_message(body, status))
        return {"started": True, "row": _row_brief(body["result"]["row"]),
                "note": "queued; the transfer compares the sha256 you gave before the book "
                        "counts as added"}
    if not arguments.get("confirm"):
        status, body = cs.action("preview", {"url": url})
        if status != 200:
            raise _ToolError(_error_message(body, status))
        return {"started": False, "preview": body["result"]["preview"], "needs_confirm": True,
                "note": "nothing was fetched. Call add_book again with confirm:true to start the "
                        "download, or supply the sha256 to skip the round-trip"}
    # confirm without a sha: preview then download in one shot. The gate wants a successful preview
    # of the same URL in this process before a download starts; doing the preview here keeps the
    # tool one-shot for the caller instead of a protocol the caller has to remember.
    status, body = cs.action("preview", {"url": url})
    if status != 200:
        raise _ToolError(_error_message(body, status))
    status, body = cs.action("download_url", {"url": url})
    if status != 200:
        raise _ToolError(_error_message(body, status))
    return {"started": True, "row": _row_brief(body["result"]["row"]),
            "note": "queued; the transfer verifies the sidecar's checksum when it carries one, "
                    "and the ZIM header otherwise -- give sha256 for a full check"}


def _call_tool(cs, name: str, arguments: dict) -> dict:
    if name == "search":
        payload = _tool_search(cs, arguments)
    elif name == "list_books":
        payload = _tool_list_books(cs)
    elif name == "add_book":
        payload = _tool_add_book(cs, arguments)
    else:
        raise _ToolError(f"no tool {name!r}; the tools are: {', '.join(TOOL_NAMES)}")
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


# -- the protocol ---------------------------------------------------------------

def handle(payload, cs):
    """One JSON-RPC message (or a batch array) in, the response object out — `None` when there is
    nothing to answer (a notification), which the handler turns into a 202 with an empty body."""
    if isinstance(payload, list):
        if not payload:
            return _err(None, -32600, "invalid request: an empty batch answers nothing")
        responses = [handle(p, cs) for p in payload]
        answered = [r for r in responses if r is not None]
        # A batch of nothing but notifications answers nothing (the handler makes it a 202).
        return answered if answered else None
    if not isinstance(payload, dict):
        return _err(None, -32600, "invalid request: a JSON-RPC 2.0 object or array")
    msg_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0":
        return _err(msg_id, -32600, 'invalid request: "jsonrpc" must be exactly "2.0"')
    method = payload.get("method")
    if method is None:
        if "result" in payload or "error" in payload:
            return None  # a client-to-server response; stateless, so there is nothing to do
        return _err(msg_id, -32600, "invalid request: wants a method")
    if msg_id is None:
        return None  # a notification: accept it, answer nothing
    if method == "initialize":
        # The version this server supports is the answer; echoing the client's would tell
        # it what it wanted rather than what is true.
        return _ok(msg_id, {"protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": SERVER_NAME, "version": __version__}})
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": tools(cs)})
    if method == "tools/call":
        params = payload.get("params") or {}
        if not isinstance(params, dict) or not params.get("name"):
            return _err(msg_id, -32602, "tools/call wants {name, arguments}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            # A string here escaped as an AttributeError and dropped the connection;
            # the spec's answer is a -32602 error object.
            return _err(msg_id, -32602, "tools/call wants an object in arguments")
        try:
            return _ok(msg_id, _call_tool(cs, str(params["name"]), arguments))
        except _ToolError as exc:
            return _ok(msg_id, {"content": [{"type": "text", "text": f"siw: {exc.message}"}],
                               "isError": True})
    return _err(msg_id, -32601, f"method {method!r} not found")
