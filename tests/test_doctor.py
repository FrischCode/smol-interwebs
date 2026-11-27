"""The doctor: one check per exit code, each with a finding that says what to change.

The exit codes are a published contract — a boot script branches on them — so each is tested on its own,
with the engine stubbed (the battery never opens a port) and a real corpus directory on disk (the listing
is the thing the doctor reads, so it is not faked). The other half of the contract, that the codes mean
what the table says on a live server, is the run recorded with the development history.

Two properties the whole module depends on are checked here rather than in prose: it reports without
starting anything, and a zero-hit smoke query is health rather than a fault — a check that cries wolf
teaches the operator to ignore the wolf.
"""
import json

import pytest

from siw import config as config_module
from siw import corpus, doctor, engine, search


@pytest.fixture
def box(tmp_path):
    """A corpus of two books, an executable that exists, a stubbed engine — and the knobs a test flips."""
    folder = tmp_path / "zims"
    folder.mkdir()
    (folder / "devdocs_en_rust_2026-10.zim").write_bytes(b"." * 40)
    (folder / "wiki_en_all_2026-01.zim").write_bytes(b"." * 60)
    (folder / "half_download.zim.part1").write_bytes(b"x")
    exe = tmp_path / "kiwix-serve.exe"
    exe.write_bytes(b"")
    conf = config_module.Config({"corpus": str(folder), "exe": str(exe), "port": 18099,
                                 "api_port": 18098, "host": "127.0.0.1", "bind": "127.0.0.1"},
                                path=tmp_path / "siw" / "config.json")
    state = {"up": True, "bound": ["127.0.0.1"], "record": None,
             "catalog": ([{"id": "uuid-1", "name": "n", "title": "t", "language": "eng",
                           "article_count": "10", "content_name": b.name}
                          for b in corpus.list_books(folder)], "")}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(engine, "probe", lambda *_a, **_k: state["up"])
    monkey.setattr(engine, "listening_on", lambda *_a, **_k: state["bound"])
    monkey.setattr(engine, "catalog", lambda *_a, **_k: state["catalog"])
    monkey.setattr(engine, "version", lambda *_a, **_k: ("kiwix-tools 3.8.1", ""))
    monkey.setattr(engine, "platform_support", lambda: (True, "win32: measured branch"))
    monkey.setattr(engine, "read_pidfile",
                   lambda *_a, **_k: (state["record"], tmp_path / "siw" / "siw-engine-18099.pid"))
    yield conf, state, folder
    monkey.undo()


def check(conf, **kwargs):
    """The report, as (code, joined text) — the two things every caller reads."""
    code, lines = doctor.report(conf, **kwargs)
    return code, "\n".join(lines)


def test_healthy_is_zero_and_prints_the_evidence(box):
    conf, _state, _folder = box
    code, text = check(conf)
    assert code == doctor.EXIT_OK
    assert "2 complete books" in text and "partial downloads seen and excluded: 1" in text
    assert "same set" in text and "finding:" not in text


def test_no_corpus_is_a_corpus_finding_and_names_the_remedy(box):
    conf, _state, _folder = box
    conf.set("corpus", None)
    code, text = check(conf)
    assert code == doctor.EXIT_CORPUS and "Settings pane" in text


def test_a_corpus_path_that_is_not_a_directory_says_exactly_that(box):
    conf, _state, _folder = box
    conf.set("corpus", "does/not/exist")
    code, text = check(conf)
    assert code == doctor.EXIT_CORPUS and "not a directory" in text


def test_a_directory_of_nothing_but_partial_downloads_is_not_a_corpus(box, tmp_path):
    """A corpus mid-download looks like a corpus and serves nothing: the download suffix is named in the
    finding, because "no books" next to a 40 GB folder reads as a bug in the tool otherwise."""
    conf, _state, _folder = box
    empty = tmp_path / "downloading"
    empty.mkdir()
    (empty / "big.zim.part1").write_bytes(b"x")
    conf.set("corpus", str(empty))
    code, text = check(conf)
    assert code == doctor.EXIT_CORPUS and "complete .zim" in text and "unfinished download" in text


def test_a_pick_naming_a_book_that_is_not_on_disk_is_refused_by_name(box):
    """The pick is an operator's act, and a name that no longer exists is a launch that serves a different
    corpus than the file says. It is a finding, not a shrug, and it names the book."""
    conf, _state, _folder = box
    conf.set("selected", ["devdocs_en_rust_2026-10", "gone_en_all_2020-01"])
    code, text = check(conf)
    assert code == doctor.EXIT_CORPUS and "gone_en_all_2020-01" in text and "not on disk" in text


def test_no_binary_is_its_own_code_and_stops_the_chain(box):
    """A missing binary is not an engine-down finding: the port is irrelevant when there is nothing to
    launch, and the chain stops where the explanation is."""
    conf, _state, _folder = box
    conf.set("exe", None)
    code, text = check(conf)
    assert code == doctor.EXIT_BINARY and "kiwix-serve" in text
    assert "engine" not in text.split("binary")[0], "nothing below the binary was checked"


def test_a_binary_that_does_not_answer_version_is_the_same_code(box, monkeypatch):
    """A path that exists but is not the engine (a directory, a stale copy) is found by `--version`, and
    the engine's own words are what make it diagnosable."""
    conf, _state, _folder = box
    monkeypatch.setattr(engine, "version", lambda _exe: ("", "no binary at 'x'"))
    code, text = check(conf)
    assert code == doctor.EXIT_BINARY and "no binary" in text


def test_a_dark_port_is_engine_down_with_the_command_that_fixes_it(box):
    conf, state, _folder = box
    state["up"] = False
    state["bound"] = []
    code, text = check(conf)
    assert code == doctor.EXIT_ENGINE_DOWN
    assert "siw serve" in text and "18099" in text and "<nothing listening>" in text


def test_the_bind_is_reported_as_the_os_sees_it(box):
    """The difference between a private corpus and a share of the machine is one boolean, printed without
    asking the operator to read a socket listing — and an unknown listing is not the same as a safe one."""
    conf, state, _folder = box
    state["bound"] = ["0.0.0.0"]
    assert "loopback only: False" in check(conf)[1]
    state["bound"] = ["127.0.0.1"]
    assert "loopback only: True" in check(conf)[1]
    state["bound"] = []
    assert "loopback only: None" in check(conf)[1]


def test_an_engine_on_every_interface_says_more_than_a_boolean(box):
    """The exit-code table is closed at seven entries, so the one finding that is a fact about the network
    rather than a fault gets a note instead of a code — and the note carries the remedy, because "False"
    in a JSON-ish line is not something an operator acts on."""
    conf, state, _folder = box
    state["bound"] = ["0.0.0.0"]
    code, text = check(conf)
    assert code == doctor.EXIT_OK, "exposure is reported, not invented as a new code"
    assert "every interface" in text and "127.0.0.1" in text


def test_a_server_holding_a_different_set_is_drift_and_names_both_sides(box):
    """Exit 5 exists because "the subset is not the subset" has two causes — the config is wrong, or the
    server on this port is not the one the config started — and the only way to tell them apart is to print
    both sets' difference."""
    conf, state, _folder = box
    state["catalog"] = ([{"id": "u", "name": "n", "title": "t", "language": "eng",
                          "article_count": None, "content_name": "other_book_2026-01"}], "")
    code, text = check(conf)
    assert code == doctor.EXIT_DRIFT
    assert "on disk but not served" in text and "served but not configured" in text
    assert "other_book_2026-01" in text and "devdocs_en_rust_2026-10" in text


def test_a_catalog_that_cannot_be_read_stops_at_engine_down_rather_than_reporting_drift(box):
    """A port that answers with no catalog is not serving a *different* set; it is not answering the
    question. Reporting drift from an empty catalog would send the operator to the wrong file."""
    conf, state, _folder = box
    state["catalog"] = ([], "the engine's catalog answered HTTP 500")
    code, text = check(conf)
    assert code == doctor.EXIT_ENGINE_DOWN and "catalog" in text


def test_the_smoke_query_is_a_real_query_with_heal_off(box, monkeypatch):
    """The smoke check goes through the same route as `siw doctor --query WORD` — and with
    `heal=False`, because a
    health check that can start a server cannot report what the machine was doing."""
    conf, _state, _folder = box
    seen = {}

    def fake_search(_config, words, **kwargs):
        seen["words"], seen["heal"] = words, kwargs.get("heal")
        return {"route": "selection-less", "count": 1, "total": 9, "seconds": 0.2,
                "results": [{"title": "Ownership", "book": "devdocs_en_rust_2026-10"}]}

    monkeypatch.setattr(search, "search", fake_search)
    code, text = check(conf, smoke_query="photosynthesis")
    assert code == doctor.EXIT_OK
    assert seen == {"words": "photosynthesis", "heal": False}
    assert "smoke:" in text and "9 index hits" in text


def test_a_zero_hit_smoke_query_is_health_not_a_fault(box, monkeypatch):
    """An empty result is a working search over a corpus that does not hold the word. Calling it unhealthy
    would make the exit code mean "the word is rare", and the operator would stop trusting it."""
    conf, _state, _folder = box
    monkeypatch.setattr(search, "search", lambda *_a, **_k: {
        "route": "selection-less", "count": 0, "total": 0, "seconds": 0.1, "results": [],
        "zero_hit": "no results"})
    code, text = check(conf, smoke_query="qqqq")
    assert code == doctor.EXIT_OK and "zero hits" in text


def test_a_failing_smoke_query_is_its_own_code(box, monkeypatch):
    """Exit 6 says the routes are broken while the port is up — the state a plain probe cannot see, and
    the reason the smoke query exists."""
    conf, _state, _folder = box

    def explode(*_a, **_k):
        raise search.SearchError("engine_rejected", "the engine answered HTTP 500", 502)

    monkeypatch.setattr(search, "search", explode)
    code, text = check(conf, smoke_query="rust")
    assert code == doctor.EXIT_SMOKE and "HTTP 500" in text


def test_the_record_and_the_log_are_printed_when_they_exist(box):
    """`siw stop` works from the record; the doctor prints where the record and the log are, so the two
    files a failed run needs are named before the run fails."""
    conf, state, _folder = box
    state["record"] = {"pid": 4242, "form": "paths", "books": 2}
    code, text = check(conf)
    assert code == doctor.EXIT_OK
    assert "started by siw: pid 4242" in text and "kiwix-serve-18099.log" in text


def test_library_ids_round_trip_through_the_cache(box, tmp_path):
    """The library launch form refuses invented ids, so the ids a catalog read learned have to survive to
    a run where nothing is up — that is the whole reason the cache exists, and a cache that does not round
    trip is a slower way of inventing nothing."""
    conf, _state, folder = box
    code, text = check(conf, library_ids=True)
    assert code == doctor.EXIT_OK and "ids:" in text
    cached = json.loads((tmp_path / "siw" / "siw-books-18099.json").read_text(encoding="utf-8"))
    assert cached["devdocs_en_rust_2026-10"]["id"] == "uuid-1"
    fresh = corpus.list_books(folder)
    assert all(not book.zim_id for book in fresh)
    assert corpus.load_id_cache(fresh, tmp_path / "siw" / "siw-books-18099.json") == 2
    assert fresh[0].zim_id == "uuid-1" and fresh[0].language == "eng"
    assert all(book.zim_id for book in fresh)


def test_every_non_zero_answer_carries_a_finding_and_a_known_code(box):
    """The table is the contract: a code outside it is a bug, and a refusal without a `finding:` line is a
    refusal the operator cannot act on. Checked across the failure paths, not asserted in prose."""
    conf, state, folder = box
    conf.set("corpus", None)
    first = check(conf)
    conf.set("corpus", str(folder))
    state["up"] = False
    second = check(conf)
    for code, text in (first, second):
        assert code in doctor.EXIT_NAMES and code != doctor.EXIT_OK
        assert "finding:" in text, f"code {code} has no finding line"
    assert (first[0], second[0]) == (doctor.EXIT_CORPUS, doctor.EXIT_ENGINE_DOWN)
