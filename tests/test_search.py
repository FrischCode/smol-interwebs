"""The search route, in process: the feed parse, the chunking, the two routes, and what each zero means.

`siw.search` is the module between a query and the engine's RSS feed, and every rule here is one the live
server taught somebody: the total arrives comma-grouped, `format=json` is not JSON on this engine family,
a scoped question must never be answered with a global one, and a zero that does not say what it excludes
is the most expensive string in the tool.

Nothing in this file opens a socket. `engine.get` is replaced with a recorder, so "which request would
this query make" is an assertion rather than a port to wait for — and the byte budget, the chunking and
the merge are checked against the request the caller *would* have sent.
"""
import pytest

from siw import config as config_module
from siw import corpus, engine, search


def feed(total, items):
    """A feed in the engine's shape: an OpenSearch total and `<item>` blocks with a `/content/` link."""
    body = "".join(f"<item><title>{title}</title><link>/content/{book}/some/path</link>"
                   f"<description>{snippet}</description></item>" for title, book, snippet in items)
    return f"<feed><totalResults>{total}</totalResults><items>{body}</items></feed>"


@pytest.fixture
def library(tmp_path):
    """Three books on disk and a config aimed at them, with the engine stubbed to answer whatever a test
    hands `answer()`."""
    folder = tmp_path / "zims"
    folder.mkdir()
    for name in ("devdocs_en_rust_2026-10.zim", "devdocs_en_python_2026-10.zim",
                 "wiki_en_all_2026-01.zim"):
        (folder / name).write_bytes(b"")
    conf = config_module.Config({"corpus": str(folder), "port": 18099, "host": "127.0.0.1"},
                                path=tmp_path / "config.json")
    calls = []
    state = {"replies": [(200, feed("0", []))]}

    def fake_get(_config, path, timeout=60.0):
        calls.append(path)
        index = min(len(calls) - 1, len(state["replies"]) - 1)
        return state["replies"][index]

    monkey = pytest.MonkeyPatch()
    monkey.setattr(engine, "get", fake_get)
    monkey.setattr(engine, "heal", lambda *_a, **_k: (True, 0.0, ""))
    monkey.setattr(engine, "catalog", lambda *_a, **_k: ([{"content_name": "x"}], ""))
    monkey.setattr(search.time, "sleep", lambda *_a: None)
    state["answer"] = lambda *replies: state.__setitem__("replies", list(replies))
    yield conf, corpus.list_books(folder), calls, state
    monkey.undo()


def ask(state, config, words="rust", **kwargs):
    """A query against the stub, with the replies for this test installed first."""
    if "replies" in kwargs:
        state["answer"](*kwargs.pop("replies"))
    return search.search(config, words, **kwargs)


def test_the_feed_total_loses_its_commas_and_names_the_book_it_came_from():
    """`totalResults` is comma-grouped text and the book is only in the link; both are facts about the
    engine's feed, and a record that quotes "1,000" as a total or loses the book is a record an adapter
    cannot rank or cite."""
    total, items = search.parse_feed(feed("1,234", [("Rust <b>ownership</b>", "devdocs_en_rust_2026-10",
                                                    "the &quot;borrow checker&quot;")]))
    assert total == 1234
    assert items[0]["title"] == "Rust ownership"
    assert items[0]["book"] == "devdocs_en_rust_2026-10"
    assert items[0]["url"] == "/content/devdocs_en_rust_2026-10/some/path"
    assert '"' in items[0]["snippet"] and "&quot;" not in items[0]["snippet"]


def test_an_entity_the_book_wrote_is_not_decoded_twice():
    """A snippet is quoted text. `&amp;lt;` is the book saying "the four characters `&lt;`", and decoding
    it a second time invents markup the source never held — the reason the numeric entities go first and
    `&amp;` goes last."""
    assert search.decode_entities("&amp;lt;b&amp;gt;") == "&lt;b&gt;"
    assert search.decode_entities("&#x41;&#66;") == "AB"
    assert search.decode_entities("&#notanumber;") == "&#notanumber;"


def test_a_selection_is_chunked_under_the_request_budget(library):
    """Names are chunked against the *encoded request*, not a book count: a chunk of N filenames is a
    different number of bytes on every corpus, and the wall it is measured against is a wall of bytes."""
    head = [("pattern", "x"), ("format", "xml"), ("pageLength", "5")]
    names = [f"a_very_long_book_name_number_{index}_for_a_deep_mount_2026-01" for index in range(60)]
    chunks = search.chunk_names(names, head, budget=1200)
    assert sum(len(chunk) for chunk in chunks) == len(names)
    assert [n for chunk in chunks for n in chunk] == names, "chunking must not reorder or drop"
    for chunk in chunks:
        params = list(head) + [("books.name", n) for n in chunk]
        assert search.request_bytes("/search", params) <= 1200
    assert len(chunks) > 1, "the point of the test is that the budget bites"


def test_an_unscoped_query_takes_the_selection_less_route(library):
    """No `sources`, no `corpus`: one request that names no book, because the engine's own merged ranking
    over everything it serves is the best ordering available and the cheapest request to make."""
    conf, books, calls, state = library
    state["answer"]((200, feed("42", [("Doc", "devdocs_en_rust_2026-10", "snippet")])))
    record = search.search(conf, "rust", books=books)
    assert record["route"] == "selection-less"
    assert len(calls) == 1 and "books.name" not in calls[0]
    assert "format=xml" in calls[0], "the engine answers RSS; without format= it answers the HTML template"
    assert record["total"] == 42 and record["count"] == 1


def test_sources_take_the_books_name_route_and_pass_every_name(library):
    """A scope is passed explicitly, one `books.name` per book: the engine's selection is exact and
    all-or-nothing, so the tool resolves the terms itself and asks for what it found by name."""
    conf, books, calls, state = library
    state["answer"]((200, feed("3", [("Doc", "devdocs_en_rust_2026-10", "s")])))
    record = search.search(conf, "rust", sources="devdocs", books=books)
    assert record["route"] == "books.name"
    assert calls[0].count("books.name=") == 2, "the two devdocs books, resolved client-side"


def test_a_scope_that_matches_no_book_searches_nothing(library):
    """The scope is not the whole corpus. A filter that resolves to no book is answered as nothing —
    without asking the engine at all — because the useful failure is "your filter matched no book here",
    not a global answer that looks like the scoped one the caller asked for."""
    conf, books, calls, state = library
    state["answer"]((200, feed("9999", [("Unasked", "wiki_en_all_2026-01", "s")])))
    record = search.search(conf, "rust", sources="nosuchbook", books=books)
    assert calls == [], "a scope that resolves to nothing must not reach the engine"
    assert record["total"] == 0 and record["count"] == 0
    assert "no configured book" in record["zero_hit"]


def test_a_corpus_filter_that_matches_no_book_searches_nothing_too(library):
    """Same rule, entered from `corpus=`: an empty result the caller asked for is a fact about the filter,
    and the record says which filter produced it."""
    conf, books, calls, _state = library
    record = search.search(conf, "rust", corpus_filter="nosuchfamily", books=books)
    assert calls == []
    assert record["count"] == 0 and "nosuchfamily" in record["zero_hit"]


def test_the_global_zero_says_what_a_global_zero_does_not_mean(library):
    """The most valuable sentence in the tool: a zero over all sources is a corpus-level claim, and the
    record has to carry the advice that goes with it — loosen the query, because the index ANDs the terms."""
    conf, books, _calls, state = library
    state["answer"]((200, feed("0", [])))
    record = search.search(conf, "photosynthesis", books=books)
    assert record["zero_hit"] == search.ZERO_HIT_GLOBAL.format(query="photosynthesis")


def test_the_scoped_zero_names_the_scope_it_failed_in(library):
    """A scoped zero is a fact about the selection, not about the corpus, and the sentence says so —
    pointing at the one remedy that can settle it, the global route."""
    conf, books, _calls, state = library
    state["answer"]((200, feed("0", [])))
    record = search.search(conf, "quux", sources="rust", books=books)
    assert record["zero_hit"].startswith("siw: no results for 'quux' in ")
    assert "NOT IN THESE SOURCES" in record["zero_hit"]


def test_a_rejected_search_is_a_failure_not_an_empty_answer(library):
    """A 409 that names the engine's own complaint beats an empty result list: "no results" and "the
    server does not hold a book you asked for" are different statements and an adapter must not have to
    guess which one it got."""
    conf, books, _calls, state = library
    state["answer"]((400, "Error 400: No such book 'wiki_en_all_2026-01'"))
    with pytest.raises(search.SearchError) as got:
        search.search(conf, "rust", sources="wiki", books=books)
    assert got.value.status == 409
    assert "restart the engine" in str(got.value).lower()


def test_an_engine_that_cannot_be_healed_is_refused_before_the_request(library):
    """`heal` is the hook, and a failed heal is a 503 rather than a connection error: the difference is
    that the caller learns the endpoint is down, in those words."""
    conf, books, calls, _state = library
    monkey = pytest.MonkeyPatch()
    monkey.setattr(engine, "heal", lambda *_a, **_k: (False, 12.0, "port never answered"))
    with pytest.raises(search.SearchError) as got:
        search.search(conf, "rust", books=books)
    monkey.undo()
    assert got.value.status == 503 and "port never answered" in str(got.value)
    assert calls == []


def test_a_first_touch_that_400s_is_retried_and_then_falls_back(library):
    """A selection-less 400 right after a launch is the measured race (400 then 200), and only that
    window re-asks: the launch is recent when `heal` reports seconds, and then the retry rides the
    same route down to the explicit-selection fallback."""
    conf, books, calls, state = library
    monkey = pytest.MonkeyPatch()
    monkey.setattr(engine, "heal", lambda *_a, **_k: (True, 1.0, ""))
    state["answer"]((400, "nope"), (400, "nope again"), (200, feed("7", [("Doc", "wiki_en_all_2026-01",
                                                                         "s")])))
    record = search.search(conf, "rust", books=books)
    monkey.undo()
    assert record["route"] == "books.name (fallback)", "the record names the request that answered"
    assert "books.name=" in calls[-1]
    assert record["total"] == 7


def test_a_persistent_400_costs_no_retry_when_the_engine_was_not_just_launched(library):
    """The retry exists for the launch window only: a server that answers 400 steadily used to cost
    2 s per request before the honest answer, once per chunk on the fan-out. Steady state asks each
    request exactly once."""
    conf, books, calls, state = library
    state["answer"]((400, "nope"))
    import pytest as _pytest
    with _pytest.raises(search.SearchError) as got:
        search.search(conf, "rust", books=books)
    assert got.value.code == "engine_rejected"
    assert len(calls) == 2  # selection-less once, the fan-out chunk once -- no re-asks


def test_the_chunks_are_merged_by_rank_not_by_chunk(library):
    """No cross-index score is exposed, so the merge is a round-robin of the per-chunk top lists: a
    rank-1 hit in the second chunk must not sit below the whole first chunk. The byte budget is squeezed
    so the chunking bites against three ordinary names — the rule under test is the merge, not the size."""
    conf, books, _calls, _state = library
    monkey = pytest.MonkeyPatch()
    monkey.setattr(search, "REQUEST_BYTE_BUDGET", 120)
    monkey.setattr(engine, "get", lambda *_a, **_k: (200, feed("5", [("rank0", books[0].name, "s"),
                                                                    ("rank1", books[0].name, "s")])))
    merged, total = search._fanout(conf, "rust", [b.name for b in books], 4, 5.0)
    monkey.undo()
    assert [item["title"] for item in merged] == ["rank0", "rank0", "rank0", "rank1"]
    assert total == 15, "the totals of the chunks are the totals of the scope"


def test_the_record_carries_the_facts_an_adapter_prints(library):
    """The record is the product: what was asked, what route answered, what was in scope, what the index
    total was, whether the endpoint had to be healed, and how long it took. A rendering can be rewritten;
    a record that cannot answer those questions forces a re-run to find out."""
    conf, books, _calls, state = library
    state["answer"]((200, feed("1", [("Doc", "devdocs_en_rust_2026-10", "s")])))
    record = search.search(conf, "rust", books=books)
    for field in ("query", "route", "scope", "total", "count", "asked", "results", "healed_in_seconds",
                  "heal_note", "seconds", "zero_hit", "endpoint"):
        assert field in record, field
    assert record["scope"]["configured"] == 3 and record["scope"]["served"] == 1
    assert record["endpoint"] == "http://127.0.0.1:18099"


def test_the_limit_is_clamped_to_what_the_engine_will_answer(library):
    """A limit of 5,000 is not a request, it is a hang: the clamp is the contract, and `asked` records
    what was actually asked so the record does not overstate the caller's patience."""
    conf, books, calls, state = library
    state["answer"]((200, feed("0", [])))
    record = search.search(conf, "rust", limit=5000, books=books)
    assert "pageLength=25" in calls[0] and record["asked"] == 25
    assert search.search(conf, "rust", limit=0, books=books)["asked"] == 1


# -- the grouped (per-book) route --------------------------------------------------

def test_the_grouped_route_asks_each_book_its_own_question_and_orders_by_hit_count(library):
    """The dominance fix, in process: a global ranking is the big index's ranking, so the grouped
    route asks each book alone and orders the answers by the hits each book got — the number that
    makes the 4,000,000-vs-3 contrast legible. The zero-hit book stays out of the results and is
    counted instead, and every result carries its own book's total."""
    conf, books, calls, state = library
    import re as _re
    per_book = {
        "wiki_en_all_2026-01": (200, feed("4,000,000", [("Decorators (design pattern)", "wiki_en_all_2026-01", "in software…")])),
        "devdocs_en_python_2026-10": (200, feed("3", [("@decorator", "devdocs_en_python_2026-10", "a function that wraps…")])),
        "devdocs_en_rust_2026-10": (200, feed("0", [])),
    }
    monkey = pytest.MonkeyPatch()
    def keyed_get(_config, path, timeout=60.0):
        calls.append(path)
        book = _re.search(r"books\.name=([^&]+)", path).group(1)
        return per_book[book]
    monkey.setattr(engine, "get", keyed_get)
    record = search.search(conf, "decorator", books=books, mode="grouped", limit=1)
    monkey.undo()
    assert record["route"] == "grouped (per-book)"
    assert len(calls) == 3 and all("books.name=" in call for call in calls)
    assert record["count"] == 2, \
        "a named universe is never sliced: limit=1 did not cut the answer, the caller chose the set"
    assert [item["book"] for item in record["results"]] == ["wiki_en_all_2026-01",
                                                             "devdocs_en_python_2026-10"], \
        "most-hit book first; the python docs' best answer surfaces, not wikipedia's millionth"
    assert record["results"][0]["hits_in_book"] == 4_000_000
    assert record["results"][1]["hits_in_book"] == 3
    assert record["total"] == 4_000_003
    assert record["books_asked"] == 3 and record["books_zero"] == 1 and record["book_errors"] == []


def test_an_unnamed_universe_answers_the_top_limit_books_by_hit_count(library):
    """No `books=`: the universe is the configured scope and the answer is the topic map — the
    top-`limit` books by hit count, not every book in the corpus. The same call with a named universe
    answers every named book, and that is the difference the record's `asked` tells."""
    conf, books, _calls, _state = library
    import re as _re
    monkey = pytest.MonkeyPatch()
    def hit_count_get(_config, path, timeout=60.0):
        book = _re.search(r"books\.name=([^&]+)", path).group(1)
        total = {"wiki_en_all_2026-01": 900, "devdocs_en_python_2026-10": 7,
                 "devdocs_en_rust_2026-10": 3}[book]
        return 200, feed(str(total), [(f"hit {total}", book, "s")])
    monkey.setattr(engine, "get", hit_count_get)
    record = search.search(conf, "rust", mode="grouped", limit=2)  # books=None: the whole scope
    monkey.undo()
    assert [item["book"] for item in record["results"]] == ["wiki_en_all_2026-01",
                                                            "devdocs_en_python_2026-10"]
    assert record["asked"] == 2 and record["count"] == 2 and record["books_asked"] == 3


def test_the_grouped_route_refuses_to_mix_with_the_selection_spellings(library):
    """`sources`/`corpus` are selection spellings for the global path; a grouped query takes its
    universe from `books`, and asking both is a caller bug the route names rather than guesses at."""
    conf, books, calls, _state = library
    with pytest.raises(search.SearchError) as got:
        search.search(conf, "rust", books=books, mode="grouped", sources="devdocs")
    assert got.value.status == 400 and got.value.code == "bad_mode" and calls == []


def test_a_grouped_zero_across_every_book_says_it_asked_per_book(library):
    conf, books, _calls, _state = library
    monkey = pytest.MonkeyPatch()
    monkey.setattr(engine, "get", lambda *_a, **_k: (200, feed("0", [])))
    record = search.search(conf, "quux", books=books, mode="grouped")
    monkey.undo()
    assert record["count"] == 0 and record["books_zero"] == 3
    assert "per-book" in record["zero_hit"] and "corpus-level zero" in record["zero_hit"]


def test_a_book_the_engine_does_not_serve_is_reported_not_swallowed(library):
    """A per-book request the engine rejects (a book added or removed since the engine loaded) is an
    error line in the record, and the other books' answers still stand — a failure that silently
    shrinks the universe reads as a corpus-level zero."""
    conf, books, _calls, _state = library
    import re as _re
    monkey = pytest.MonkeyPatch()
    def partial_get(_config, path, timeout=60.0):
        book = _re.search(r"books\.name=([^&]+)", path).group(1)
        if book == "devdocs_en_rust_2026-10":
            return 400, "Error 400: No such book 'devdocs_en_rust_2026-10'"
        return 200, feed("2", [("hit", book, "s")])
    monkey.setattr(engine, "get", partial_get)
    record = search.search(conf, "rust", books=books, mode="grouped")
    monkey.undo()
    assert len(record["book_errors"]) == 1 and "devdocs_en_rust_2026-10" in record["book_errors"][0]
    assert record["count"] == 2 and record["books_zero"] == 0


# -- the empty query --------------------------------------------------------------

def test_an_empty_query_is_refused_before_anything_is_asked():
    """An empty query against a corpus is "give me everything", which is not a search and is expensive to
    answer; it is refused with the code the API answers it with."""
    with pytest.raises(search.SearchError) as got:
        search.search(config_module.Config({}), "   ")
    assert got.value.status == 400


def test_a_socket_error_on_one_book_is_that_book_s_error_not_the_end_of_the_request(library):
    """One book's connection reset used to escape `ask` and take every answer already
    collected with it -- the route's promise is that a per-book failure is a per-book
    error line, whatever the failure is."""
    conf, books, _calls, _state = library
    import re as _re
    monkey = pytest.MonkeyPatch()
    def flaky_get(_config, path, timeout=60.0):
        book = _re.search(r"books\.name=([^&]+)", path).group(1)
        if book == "devdocs_en_rust_2026-10":
            raise OSError("connection refused")
        return 200, feed("2", [("hit", book, "s")])
    monkey.setattr(engine, "get", flaky_get)
    record = search.search(conf, "rust", books=books, mode="grouped")
    monkey.undo()
    assert len(record["book_errors"]) == 1 and "connection refused" in record["book_errors"][0]
    assert record["count"] == 2


def test_the_grouped_route_cuts_per_book_timeouts_and_runs_to_a_deadline(library):
    """The global route's 180 s budget is for one request; grouped asks every book. Each
    request must see a cut timeout, and the whole route a wall-clock deadline: a hung
    engine used to hold the request for 180 s times however many books."""
    conf, books, _calls, _state = library
    seen = []
    monkey = pytest.MonkeyPatch()
    def recording_get(_config, path, timeout=60.0):
        seen.append(timeout)
        return 200, feed("1", [("hit", "x", "s")])
    monkey.setattr(engine, "get", recording_get)
    search.search(conf, "rust", books=books, mode="grouped", timeout=180.0)
    assert seen and max(seen) <= 30.0
    spent = search.search_grouped(conf, "rust", books=books, heal=False, route_budget=0.0)
    monkey.undo()
    assert len(spent["book_errors"]) == 3
    assert all("deadline" in e for e in spent["book_errors"])
