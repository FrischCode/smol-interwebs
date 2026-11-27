"""The MCP route, in process: the protocol shape and the three tools against a faked console service.

`siw.mcp` is an adapter, so the test fakes the one thing it adapts to — a console service with a
`config`, a `state()` and an `action()` — and asserts what the protocol says on the wire: a JSON-RPC
request in, a JSON-RPC answer out, a notification in, no answer. The kernel's search is stubbed where
a tool reaches it, so no engine and no socket are needed, and the roster in the search description is
checked against the rows the fake service stands up.
"""
import json

import pytest

from siw import config as config_module
from siw import mcp


class FakeService:
    """The shape of `ConsoleService` that the adapter touches: config, state, and the verbs it calls."""

    def __init__(self, config, rows=None, actions=None):
        self.config = config
        self.rows = rows or []
        self.action_log = []
        self._actions = actions or {}

    def state(self):
        return {"engine": {"state": "up", "url": "http://127.0.0.1:18099"}, "rows": self.rows}

    def action(self, verb, args):
        self.action_log.append((verb, dict(args)))
        handler = self._actions.get(verb)
        if handler is None:
            return 404, {"ok": False, "error": {"code": "unknown_verb", "message": f"no verb {verb!r}"}}
        return handler(args)


def row(name, state="served", size=2_000_000_000, media="code", title=None):
    return {"id": name, "book": name, "title": title or name, "media": media,
            "size_bytes": size, "state": state,
            "served": {"page_url": f"http://127.0.0.1:18099/content/{name}/"} if state == "served" else None}


def corpus_with(tmp_path, *names):
    """A real corpus directory when a test needs name resolution; a bare config when it does not."""
    if tmp_path is None:
        return config_module.Config({})
    folder = tmp_path / "zims"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / f"{name}.zim").write_bytes(b"")
    return config_module.Config({"corpus": str(folder), "port": 18099, "host": "127.0.0.1"},
                                 path=tmp_path / "config.json")


def rpc(method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


# -- the protocol -----------------------------------------------------------------

def test_initialize_answers_with_the_protocol_version_and_server_identity():
    service = FakeService(corpus_with(None, "a"))
    reply = mcp.handle(rpc("initialize", {"protocolVersion": mcp.PROTOCOL_VERSION,
                                          "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}),
                       service)
    assert reply["id"] == 1 and reply["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert reply["result"]["serverInfo"]["name"] == mcp.SERVER_NAME
    assert reply["result"]["capabilities"]["tools"]["listChanged"] is False


def test_a_notification_is_accepted_and_answers_nothing():
    service = FakeService(corpus_with(None, "a"))
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, service) is None


def test_an_unknown_method_is_a_protocol_error_not_a_tool_error():
    service = FakeService(corpus_with(None, "a"))
    reply = mcp.handle(rpc("resources/list"), service)
    assert reply["error"]["code"] == -32601 and "resources/list" in reply["error"]["message"]


def test_a_batch_is_answered_per_message_with_notifications_dropped():
    service = FakeService(corpus_with(None, "a"))
    batch = [rpc("ping", None, 1), {"jsonrpc": "2.0", "method": "notifications/initialized"},
             rpc("ping", None, 3)]
    reply = mcp.handle(batch, service)
    assert [r["id"] for r in reply] == [1, 3], "a batch answers every request and no notification"


def test_a_body_that_is_not_json_rpc_is_an_invalid_request():
    service = FakeService(corpus_with(None, "a"))
    assert mcp.handle("a string", service)["error"]["code"] == -32600
    assert mcp.handle({"jsonrpc": "2.0"}, service)["error"]["code"] == -32600


# -- tools/list ---------------------------------------------------------------------

def test_tools_list_carries_the_live_roster_in_the_search_description():
    service = FakeService(corpus_with(None, "a"), rows=[
        row("wiki_en_all_2026-01", size=107_000_000_000, media="prose", title="Wikipedia (English)"),
        row("devdocs_en_python_2026-10"),
        row("ifixit_2026-01", state="downloaded"),
    ])
    reply = mcp.handle(rpc("tools/list"), service)
    tools = {t["name"]: t for t in reply["result"]["tools"]}
    assert set(tools) == {"search", "list_books", "add_book"}
    description = tools["search"]["description"]
    assert "wiki_en_all_2026-01" in description and "107 GB" in description, \
        "the served set, as it stands right now, is what the description says"
    assert "ifixit_2026-01" not in description, "a downloaded-not-served book is not queryable yet"
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"


def test_the_roster_says_no_books_when_none_are_served():
    service = FakeService(corpus_with(None, "a"), rows=[row("a", state="absent")])
    tools = {t["name"]: t for t in mcp.handle(rpc("tools/list"), service)["result"]["tools"]}
    assert "no books served yet" in tools["search"]["description"]


# -- tools/call: search ----------------------------------------------------------------

def test_search_dispatches_to_the_kernel_with_the_resolved_book_list(monkeypatch, tmp_path):
    """`books=` is resolved against the corpus before the kernel sees it, and an unknown name is a
    tool error that names it — the adapter is where the caller's spelling meets this machine's stems."""
    conf = corpus_with(tmp_path, "devdocs_en_python_2026-10", "devdocs_en_rust_2026-10")
    seen = {}

    def fake_search(_cfg, query, **kwargs):
        seen["query"] = query
        seen.update(kwargs)
        return {"query": query, "route": "grouped (per-book)", "results": []}

    monkeypatch.setattr(mcp, "run_search", fake_search)
    service = FakeService(conf)
    reply = mcp.handle(rpc("tools/call", {"name": "search",
                                          "arguments": {"query": "decorator", "mode": "grouped",
                                                        "books": ["devdocs_en_python_2026-10"],
                                                        "limit": 3}}), service)
    result = reply["result"]
    assert result["isError"] is False
    assert seen["query"] == "decorator" and seen["limit"] == 3 and seen["mode"] == "grouped"
    assert [b.name for b in seen["books"]] == ["devdocs_en_python_2026-10"], \
        "the names became the Book objects the kernel speaks"

    reply = mcp.handle(rpc("tools/call", {"name": "search",
                                          "arguments": {"query": "x", "mode": "grouped",
                                                        "books": ["nosuchbook"]}}), service)
    assert reply["result"]["isError"] is True and "nosuchbook" in reply["result"]["content"][0]["text"]

    # Un-named grouped: the kernel takes the configured scope (books=None), not the adapter's list.
    seen.clear()
    reply = mcp.handle(rpc("tools/call", {"name": "search",
                                          "arguments": {"query": "x", "mode": "grouped"}}), service)
    assert reply["result"]["isError"] is False and seen["books"] is None


def test_search_refuses_books_in_the_global_mode():
    """The global route is the engine's own merged ranking over everything served; narrowing its
    universe with `books=` is asking the grouped question with the global spelling."""
    service = FakeService(corpus_with(None, "a", "b"))
    reply = mcp.handle(rpc("tools/call", {"name": "search",
                                          "arguments": {"query": "x", "mode": "global", "books": ["a"]}}),
                       service)
    assert reply["result"]["isError"] is True and "mode=grouped" in reply["result"]["content"][0]["text"]


def test_a_kernel_rejection_surfaces_as_a_tool_error_with_its_code(monkeypatch, tmp_path):
    from siw.search import SearchError

    def refuse(_cfg, query, **_kwargs):
        raise SearchError("engine_down", "port never answered", 503)

    monkeypatch.setattr(mcp, "run_search", refuse)
    service = FakeService(corpus_with(tmp_path, "a"))
    reply = mcp.handle(rpc("tools/call", {"name": "search", "arguments": {"query": "x"}}), service)
    assert reply["result"]["isError"] is True and "engine_down" in reply["result"]["content"][0]["text"]


def test_a_missing_query_is_a_tool_error_before_anything_runs():
    service = FakeService(corpus_with(None, "a"))
    reply = mcp.handle(rpc("tools/call", {"name": "search", "arguments": {"query": "  "}}), service)
    assert reply["result"]["isError"] is True and "query is required" in reply["result"]["content"][0]["text"]


# -- tools/call: list_books -------------------------------------------------------------

def test_list_books_reports_each_book_with_its_state_and_page_url():
    service = FakeService(corpus_with(None, "a"), rows=[
        row("wiki_en_all_2026-01", size=107_000_000_000, title="Wikipedia (English)"),
        row("devdocs_en_python_2026-10", state="downloading"),
        row("ifixit_2026-01", state="absent"),
    ])
    reply = mcp.handle(rpc("tools/call", {"name": "list_books", "arguments": {}}), service)
    result = json.loads(reply["result"]["content"][0]["text"])
    assert result["served"] == 1
    by_name = {b["name"]: b for b in result["books"]}
    assert by_name["wiki_en_all_2026-01"]["page_url"].endswith("/content/wiki_en_all_2026-01/")
    assert by_name["devdocs_en_python_2026-10"]["state"] == "downloading"
    assert by_name["devdocs_en_python_2026-10"]["page_url"] is None
    assert result["engine"]["state"] == "up"


# -- tools/call: add_book -----------------------------------------------------------------

def test_add_book_previews_first_and_confirms_on_the_second_call():
    """The preview gate, as a tool: the first call fetches nothing and asks for the confirm; the
    second call previews (the gate's own requirement, in this process) and then starts the transfer."""
    def preview(args):
        return 200, {"ok": True, "result": {"preview": {"url": args["url"], "book": "wiki_fr_all",
                                                        "size_bytes": 30_000_000_000,
                                                        "sha256": "ab" * 32, "title": "Wikipedia (French)"}}}
    def download(args):
        return 200, {"ok": True, "result": {"row": {"id": "wiki_fr_all", "book": "wiki_fr_all",
                                                    "title": "Wikipedia (French)",
                                                    "size_bytes": 30_000_000_000, "state": "queued",
                                                    "url": args["url"]}}}
    service = FakeService(corpus_with(None, "a"),
                          actions={"preview": preview, "download_url": download})
    url = "https://download.kiwix.org/zim/wikipedia/wikipedia_fr_all_nopic_2026-01.zim"

    reply = mcp.handle(rpc("tools/call", {"name": "add_book", "arguments": {"url": url}}), service)
    first = json.loads(reply["result"]["content"][0]["text"])
    assert reply["result"]["isError"] is False
    assert first["started"] is False and first["needs_confirm"] is True
    assert first["preview"]["size_bytes"] == 30_000_000_000
    assert service.action_log == [("preview", {"url": url})], "nothing is fetched before the confirm"

    reply = mcp.handle(rpc("tools/call", {"name": "add_book", "arguments": {"url": url, "confirm": True}}),
                       service)
    second = json.loads(reply["result"]["content"][0]["text"])
    assert second["started"] is True and second["row"]["state"] == "queued"
    assert [verb for verb, _args in service.action_log] == ["preview", "preview", "download_url"], \
        "the confirm previews in this process and then downloads: the gate is satisfied, not bypassed"


def test_add_book_with_a_sha_starts_on_the_first_call():
    """A checksum that came with the request satisfies the preview gate: nothing is fetched before
    it is checked, so the download is one round-trip."""
    def download(args):
        return 200, {"ok": True, "result": {"row": {"id": "x", "book": "x", "title": "x",
                                                    "size_bytes": 1, "state": "queued", "url": args["url"]}}}
    service = FakeService(corpus_with(None, "a"), actions={"download_url": download})
    reply = mcp.handle(rpc("tools/call", {"name": "add_book",
                                          "arguments": {"url": "https://x/y.zim", "sha256": "ab" * 32}}),
                       service)
    assert json.loads(reply["result"]["content"][0]["text"])["started"] is True
    assert service.action_log == [("download_url", {"url": "https://x/y.zim", "sha256": "ab" * 32})]


def test_a_bad_sha_is_refused_before_any_verb_runs():
    service = FakeService(corpus_with(None, "a"), actions={"download_url": lambda a: (200, {})})
    reply = mcp.handle(rpc("tools/call", {"name": "add_book",
                                          "arguments": {"url": "https://x/y.zim", "sha256": "zz" * 32}}),
                       service)
    assert reply["result"]["isError"] is True and "64 hex" in reply["result"]["content"][0]["text"]
    assert service.action_log == []


def test_a_gate_rejection_surfaces_with_the_verb_s_words():
    """A 409 preview_required is the console's refusal, and the tool carries its message verbatim —
    an error the adapter rewrites in its own words hides the reason from the caller."""
    # The refusal is pointed at directly: the preview verb itself refuses.
    service = FakeService(corpus_with(None, "a"),
                          actions={"preview": lambda args: (400, {"ok": False,
                                                                  "error": {"code": "unrecognized_url",
                                                                            "message": "the paste field takes a .zim URL"}})})
    reply = mcp.handle(rpc("tools/call", {"name": "add_book", "arguments": {"url": "ftp://nope"}}), service)
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True and "unrecognized_url" in text and "400" in text


def test_an_unknown_tool_is_a_tool_error_that_names_the_tools():
    service = FakeService(corpus_with(None, "a"))
    reply = mcp.handle(rpc("tools/call", {"name": "warp", "arguments": {}}), service)
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] is True and "no tool 'warp'" in text
    for tool in ("search", "list_books", "add_book"):
        assert tool in text


def test_an_empty_batch_is_an_invalid_request():
    """JSON-RPC says an empty batch answers nothing; answering *nothing* with *nothing* is
    indistinguishable from a lost reply -- the spec's own answer is -32600."""
    reply = mcp.handle([], FakeService(config_module.Config({})))
    assert reply["error"]["code"] == -32600


def test_a_batch_of_notifications_alone_answers_nothing():
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert mcp.handle([note, note], FakeService(config_module.Config({}))) is None


def test_a_missing_jsonrpc_version_is_an_invalid_request():
    """The spec is exact: no "jsonrpc": "2.0" means invalid request (-32600), whatever
    the method happens to be -- answering it would train clients to send malformed traffic."""
    reply = mcp.handle({"id": 1, "method": "ping"}, FakeService(config_module.Config({})))
    assert reply["error"]["code"] == -32600


def test_initialize_answers_the_version_it_supports_not_the_one_asked():
    reply = mcp.handle(rpc("initialize", {"protocolVersion": "0000-00-00", "capabilities": {},
                                          "clientInfo": {"name": "test"}}),
                       FakeService(config_module.Config({})))
    assert reply["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_arguments_that_is_not_an_object_is_invalid_params():
    """A string in `arguments` once escaped as an AttributeError and dropped the socket;
    the spec's answer is an error object, -32602."""
    reply = mcp.handle(rpc("tools/call", {"name": "search", "arguments": "rust"}),
                       FakeService(config_module.Config({})))
    assert reply["error"]["code"] == -32602
