"""The HTTP surface, without a socket: `Service.dispatch` is the whole contract as a pure function.

The server was written before its spec on purpose — the spec is derived from what answers — but "what
answers" was checked in two ways, and this is the first one: route in, status and payload out, with the
engine stubbed. The second way is the live cycle recorded with the development history, where the same routes are
hit with `curl` against a running server and the recorded shapes are diffed against `docs/api.json`.

The claims under test are the ones an adapter is built on: one JSON object always, one error shape, the
parameter set enforced rather than ignored, and the engine's address unreachable from a request.
"""
import json

import pytest

from siw import api, config as config_module, corpus, engine


@pytest.fixture
def service(tmp_path):
    """A server's worth of state on a three-book corpus, with every engine call stubbed to a value a test
    can see, and nothing able to reach a port."""
    folder = tmp_path / "zims"
    folder.mkdir()
    for name, size in (("devdocs_en_rust_2026-10.zim", 5), ("devdocs_en_python_2026-10.zim", 4),
                       ("wiki_en_all_2026-01.zim", 6)):
        (folder / name).write_bytes(b"." * size)
    conf = config_module.Config({"corpus": str(folder), "port": 18099, "api_port": 18098,
                                 "host": "127.0.0.1", "bind": "127.0.0.1"},
                                path=tmp_path / "config.json")
    state = {"up": True, "catalog": ([{"id": "u", "name": "n", "title": "t", "language": "eng",
                                       "article_count": None, "content_name": b.name}
                                      for b in corpus.list_books(folder)], ""),
             "bound": ["127.0.0.1"], "search": (200, api_rss())}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(engine, "probe", lambda *_a, **_k: state["up"])
    monkey.setattr(engine, "listening_on", lambda *_a, **_k: state["bound"])
    monkey.setattr(engine, "catalog", lambda *_a, **_k: state["catalog"])
    monkey.setattr(engine, "heal", lambda *_a, **_k: (True, 0.0, ""))
    monkey.setattr(engine, "get", lambda *_a, **_k: state["search"])
    monkey.setattr(engine, "read_pidfile", lambda *_a, **_k: ({"pid": 1, "form": "paths"},
                                                              tmp_path / "siw-engine-18099.pid"))
    yield api.Service(conf, spec_path=tmp_path / "api.json"), state, conf
    monkey.undo()


def api_rss(total="7"):
    body = ("<item><title>Rust <b>ownership</b></title>"
            "<link>/content/devdocs_en_rust_2026-10/book/ch04-01-what-is-ownership</link>"
            "<description>the borrow checker</description></item>")
    return f"<feed><totalResults>{total}</totalResults><items>{body}</items></feed>"


def get(service, target):
    """The one line every test uses: a GET, dispatched."""
    return service[0].dispatch("GET", target)


def test_the_card_names_every_route_and_every_error(service):
    """A curious client reads `/` first, and what it needs is the route list and the meaning of the status
    codes — the two things that let it write an adapter without reading this repo."""
    status, payload = get(service, "/")
    assert status == 200
    assert payload["service"] == "siw"
    assert set(payload["routes"]) == {"GET /health",
                                      "GET /search?q=&limit=&sources=&corpus=&heal=&mode=&books=",
                                      "GET /catalog?q=&picked=&served=", "GET /spec", "POST /mcp", "GET /"}
    assert set(payload["errors"]) == {"400", "404", "405", "409", "500", "502", "503"}


def test_health_reports_the_engine_as_it_is_not_as_it_was_configured(service):
    """The health record is read by a boot script, so the fields it branches on are the OS's own answers:
    is the port answering, what is it actually bound to, is that loopback, and does the served set match
    the configured pick."""
    status, payload = get(service, "/health")
    assert status == 200 and payload["ok"] is True
    assert payload["engine"]["up"] is True and payload["engine"]["bound_on"] == ["127.0.0.1"]
    assert payload["engine"]["loopback_only"] is True
    assert payload["engine"]["started_by_siw"] is True and payload["engine"]["form"] == "paths"
    assert payload["served"] == 3 and payload["drift"]["same"] is True
    assert payload["corpus"]["books"] == 3 and payload["corpus"]["bytes"] > 0
    assert payload["config_path"].endswith("config.json")


def test_health_says_the_engine_is_down_rather_than_omitting_it(service):
    """`ok: false` with `served: null`: a down engine and an engine whose catalog could not be read are
    different facts, and dropping the field would let a dashboard read "no data" as "no drift"."""
    _svc, state, _conf = service
    state["up"] = False
    status, payload = get(service, "/health")
    assert status == 200 and payload["ok"] is False
    assert payload["served"] is None and payload["drift"] is None
    assert payload["engine"]["catalog_problem"] == "engine down"


def test_a_server_on_every_interface_says_so(service):
    """The bind is the security story, and the boolean that says "this is a share of the machine" is worth
    more than a netstat block nobody read: `0.0.0.0` is not loopback, and an unknown listing is not either."""
    _svc, state, _conf = service
    state["bound"] = ["0.0.0.0"]
    assert get(service, "/health")[1]["engine"]["loopback_only"] is False
    state["bound"] = []
    assert get(service, "/health")[1]["engine"]["loopback_only"] is None


def test_the_search_route_answers_with_the_record(service):
    """The record is the product, and the route is what makes it reproducible: same fields as the CLI
    prints, same words, one JSON object."""
    status, payload = get(service, "/search?q=ownership&limit=2")
    assert status == 200
    assert payload["route"] == "selection-less" and payload["total"] == 7 and payload["count"] == 1
    assert payload["results"][0]["book"] == "devdocs_en_rust_2026-10"
    assert payload["scope"]["configured"] == 3


def test_a_request_cannot_name_the_engine_it_wants_searched(service):
    """The engine's address is the operator's act: a parameter that is not on the list is refused with the
    list, rather than ignored. A silently ignored argument is how a client believes it narrowed a scope
    that was never narrowed."""
    status, payload = get(service, "/search?q=rust&host=192.0.2.9&port=1&exe=C:\\evil")
    assert status == 400 and payload["error"]["code"] == "unknown_parameter"
    for name in ("exe", "host", "port"):
        assert name in payload["error"]["message"]
    message = payload["error"]["message"]
    assert "is not a parameter of /search" in message and "sources" in message and "query" in message


def test_a_missing_query_is_a_refusal_not_an_empty_search(service):
    """`/search` with no `q` is "give me everything", which is not a search; the answer names the shape
    that would work."""
    status, payload = get(service, "/search")
    assert status == 400 and payload["error"]["code"] == "missing_query"
    assert "/search?q=" in payload["error"]["message"]


def test_a_limit_that_is_not_a_number_is_named_as_one(service):
    """A 400 that quotes the offending value is a 400 the caller can fix without reading the source."""
    status, payload = get(service, "/search?q=rust&limit=many")
    assert status == 400 and payload["error"]["code"] == "bad_limit" and "many" in payload["error"]["message"]


def test_heal_zero_reaches_the_search_as_a_refusal_to_launch(service):
    """`heal=0` is the difference between a query that may start a server and one that may not, and it is
    a documented parameter, so it has to survive the trip through the route table."""
    seen = {}
    monkey = pytest.MonkeyPatch()

    def spy(*_args, **kwargs):
        seen.update(kwargs)
        raise engine.EngineError("refusing to start anything")

    monkey.setattr(api.search, "search", spy)
    status, payload = get(service, "/search?q=rust&heal=0")
    monkey.undo()
    assert seen["heal"] is False
    assert status == 503 and payload["error"]["code"] == "engine_error"


def test_heal_defaults_to_true(service):
    """The default is the one the CLI uses too: a dark endpoint is started rather than reported, and a
    client that wants the opposite has to say so."""
    seen = {}
    monkey = pytest.MonkeyPatch()

    def spy(*_args, **kwargs):
        seen.update(kwargs)
        return {"query": "rust", "results": []}

    monkey.setattr(api.search, "search", spy)
    get(service, "/search?q=rust")
    monkey.undo()
    assert seen["heal"] is True


def test_the_catalog_lists_filters_and_reports_the_cost(service):
    """The catalog route is the console's data: names, sizes, what is picked, and the whole-corpus flag —
    the four facts that make choosing a subset an informed act."""
    status, payload = get(service, "/catalog?q=devdocs")
    assert status == 200
    assert payload["count"] == 3 and payload["shown"] == 2 and payload["whole_corpus"] is True
    assert {book["name"] for book in payload["books"]} == {"devdocs_en_rust_2026-10",
                                                           "devdocs_en_python_2026-10"}
    assert payload["cost"]["count"] == 3, "the cost is the pick's, not the filter's"


def test_the_catalog_can_ask_the_engine_what_it_serves(service):
    """`served=1` is the drift check on demand: the names the running server holds, in the spelling
    `books.name=` takes."""
    status, payload = get(service, "/catalog?served=1")
    assert status == 200
    assert payload["served"] == ["devdocs_en_python_2026-10", "devdocs_en_rust_2026-10",
                                 "wiki_en_all_2026-01"]


def test_a_parameter_this_surface_accepts_changes_the_answer(service):
    """`picked=1` is accepted by the route table, so it has to filter: an accepted-and-ignored parameter is
    the exact failure the 400-on-unknown-parameter rule exists to prevent, and a client that asked for the
    pick and got the catalogue believes it is looking at the pick."""
    _svc, _state, conf = service
    conf.set("selected", ["devdocs_en_rust_2026-10"])
    everything = get(service, "/catalog")[1]
    only = get(service, "/catalog?picked=1")[1]
    assert everything["shown"] == 3 and everything["picked"] == 1
    assert only["shown"] == 1 and [b["name"] for b in only["books"]] == ["devdocs_en_rust_2026-10"]
    assert only["cost"]["count"] == 1, "the cost follows the pick, and the pick is what this shows"


def test_the_spec_is_served_from_the_file_the_server_was_built_with(service, tmp_path):
    """The document is the server's own contract, read from disk so a reviewer can diff it and a client
    can read the version that is actually answering. A staged copy is the `SIW_SPEC` case."""
    _svc, _state, _conf = service
    (tmp_path / "api.json").write_text(json.dumps({"openapi": "3.1.0", "x": 1}), encoding="utf-8")
    status, payload = get(service, "/spec")
    assert status == 200 and payload == {"openapi": "3.1.0", "x": 1}


def test_a_missing_spec_is_named_not_empty(service):
    """An agent that reads an empty spec writes an adapter against nothing, so the refusal names the path
    this server looked at."""
    status, payload = get(service, "/spec")
    assert status == 500 and payload["error"]["code"] == "spec_missing"
    assert payload["error"]["message"].endswith("api.json")


def test_a_broken_spec_says_so_with_the_parser_words(service, tmp_path):
    """The difference between "no file" and "your file is not JSON" is the difference between a deployment
    mistake and an editing mistake, and both are cheaper when the parser's own words are kept."""
    _svc, _state, _conf = service
    (tmp_path / "api.json").write_text("{not json", encoding="utf-8")
    status, payload = get(service, "/spec")
    assert status == 500 and payload["error"]["code"] == "spec_unreadable"


def test_an_unknown_route_points_at_the_route_list(service):
    status, payload = get(service, "/searchz?q=rust")
    assert status == 404 and payload["error"]["code"] == "no_route" and "GET /" in payload["error"]["message"]


def test_only_get_is_answered(service):
    """Every route is a read; a method that is not GET is refused rather than silently treated as one, and
    the answer says what would work."""
    for method in ("POST", "PUT", "DELETE"):
        status, payload = service[0].dispatch(method, "/health")
        assert status == 405, method
        assert payload["error"]["code"] == "not_get"


def test_a_trailing_slash_is_the_same_route(service):
    """Browsers and shell scripts add them; a contract with two spellings for one route is a contract with
    a gap in it."""
    assert get(service, "/health/")[0] == 200
    assert get(service, "/catalog/")[0] == 200
    assert get(service, "/search/?q=rust")[0] == 200


def test_every_failure_is_the_one_error_shape(service):
    """One shape, everywhere: `{"error": {"code", "message"}}`. An adapter writes one error path when the
    shape does not vary, and varies its behaviour on `code`."""
    for target in ("/search", "/search?q=rust&limit=x", "/nope", "/spec"):
        payload = get(service, target)[1]
        assert set(payload) == {"error"} and set(payload["error"]) == {"code", "message"}, target


def test_a_route_that_raises_a_bug_answers_500_rather_than_dropping_the_connection(service):
    """A route that throws an unexpected exception is a bug in this server, and the caller still gets a
    named JSON answer: an empty reply or a closed socket is what a client cannot distinguish from a
    network fault, and it is the reason the handler exists at all."""
    monkey = pytest.MonkeyPatch()

    def explode(*_args, **_kwargs):
        raise ZeroDivisionError("a bug, not a request")

    monkey.setattr(api.search, "search", explode)
    status, payload = get(service, "/search?q=rust")
    monkey.undo()
    assert status == 500 and payload["error"]["code"] == "crash"
    assert "ZeroDivisionError" in payload["error"]["message"]


def test_the_shipped_spec_answers_from_the_repo_tree(tmp_path):
    """B7 [repro]: GET / and the boot banner advertise /spec, and it answered 500
    spec_missing -- the document was never in the tree. Now it is, and it is valid
    JSON naming every route the server answers."""
    conf = config_module.Config({"corpus": str(tmp_path), "port": 19300, "api_port": 19301,
                                 "host": "127.0.0.1", "bind": "127.0.0.1"})
    status, payload = api.Service(conf).dispatch("GET", "/spec")
    assert status == 200, payload
    assert payload["openapi"].startswith("3.")
    for route in ("/", "/health", "/search", "/catalog", "/spec", "/action", "/mcp"):
        assert route in payload["paths"]
