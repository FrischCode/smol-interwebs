"""The corpus as a set of facts: names, sizes, the pick, the drift line, and the two launch forms.

Every test builds a throwaway folder of empty files named like real ZIMs — the code reads names and
sizes, never the bodies, and a test that needed a real ZIM could not ship with the repo.
"""
import os
from pathlib import Path

import pytest

from siw import corpus


def make(folder: Path, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"")
    return folder


def test_a_directory_of_books_is_listed_by_name(tmp_path):
    """The listing is what the console, the launch and the drift check all read, so it is sorted, it is
    complete, and it carries both names and the byte count."""
    make(tmp_path, "b_book_2026-10.zim", "a_book_2026-10.zim")
    books = corpus.list_books(tmp_path)
    assert [b.name for b in books] == ["a_book_2026-10", "b_book_2026-10"]
    assert books[0].path.endswith(".zim") and books[0].size == 0


def test_an_inflight_download_is_never_a_book(tmp_path):
    """A `*.zim.part` is a file the engine would open and pay a cold load for, and one that disappears
    mid-startup has hung this engine family rather than failing it. It is listed separately, because
    `siw doctor` prints the count: a corpus mid-download is a corpus that is not what it looks like."""
    make(tmp_path, "done_2026-10.zim", "half_2026-10.zim.part", "other_2026-10.zim.part0")
    assert [b.name for b in corpus.list_books(tmp_path)] == ["done_2026-10"]
    assert corpus.parts(tmp_path) == ["half_2026-10.zim.part", "other_2026-10.zim.part0"]


def test_a_missing_directory_is_empty_not_an_error(tmp_path):
    """A corpus path that is not there answers with an empty list — the doctor turns it into a finding
    with the path in it, which is a better answer than an exception from a listing call."""
    assert corpus.list_books(tmp_path / "absent") == []
    assert corpus.list_books(None) == []
    assert corpus.parts(tmp_path / "absent") == []


def test_the_short_name_drops_the_date_and_the_edition_but_keeps_the_language():
    """Two names, one rule: the short name is what a person types, so the date and the edition tag go and
    the language stays — `devdocs_rust` and `devdocs_python` are different books to pick between."""
    assert corpus.short_name("devdocs_en_rust_2026-10") == "devdocs_rust"
    assert corpus.short_name("wikipedia_en_all_maxi_2026-08") == "wikipedia"
    assert corpus.short_name("askubuntu.com_en_all_2026-10") == "askubuntu.com"
    assert corpus.short_name("libretexts_org_chem_2026-04") == "libretexts_org_chem"


def test_the_book_name_is_the_file_stem_because_that_is_what_the_engine_selects(tmp_path):
    """`books.name=` wants the stem with the date, and the engine's catalog `<name>` field is a different,
    dateless string — the two are kept apart here so a search selection is never built from a catalog
    label."""
    make(tmp_path, "devdocs_en_rust_2026-10.zim")
    book = corpus.list_books(tmp_path)[0]
    assert book.name == "devdocs_en_rust_2026-10"
    assert book.short_name == "devdocs_rust"


def test_the_pick_selects_by_either_name_and_keeps_the_picks_order(tmp_path):
    """The pick is an operator's list, and a tool that re-sorts it is a tool whose saved config looks
    different from what was typed. Both names resolve, so a pick survives a renamed date."""
    make(tmp_path, "one_2026-10.zim", "two_2026-10.zim", "three_2026-10.zim")
    books = corpus.list_books(tmp_path)
    picked = corpus.select(books, ["two_2026-10", "one"])
    assert [b.name for b in picked] == ["two_2026-10", "one_2026-10"]


def test_no_pick_is_the_whole_corpus_and_a_lost_pick_is_not_nothing(tmp_path):
    """`None` is "nobody narrowed it"; an empty list is a config that lost its selection. Both serve
    everything, because a corpus that silently narrows to zero books answers every query with a zero-hit
    and reads like a search problem."""
    make(tmp_path, "one_2026-10.zim", "two_2026-10.zim")
    books = corpus.list_books(tmp_path)
    assert corpus.select(books, None) == books
    assert [b.name for b in corpus.select(books, [])] == ["one_2026-10", "two_2026-10"]


def test_a_pick_that_names_a_gone_book_is_reported_not_dropped(tmp_path):
    """The stale pick is the common failure of a saved subset: a book deleted, a date advanced. Reporting
    it keeps "my subset is not my subset" visible instead of becoming a mysterious zero-hit."""
    make(tmp_path, "still_here_2026-10.zim")
    books = corpus.list_books(tmp_path)
    assert corpus.unknown_names(books, ["still_here", "gone_book"]) == ["gone_book"]
    assert corpus.unknown_names(books, ["still_here"]) == []


def test_the_substring_selector_matches_short_and_full_names(tmp_path):
    """`sources=` and the console's filter are this function, so one word means the same thing in the API
    and on the screen: it matches the typed short name and the dated stem, case-insensitively."""
    make(tmp_path, "devdocs_en_rust_2026-10.zim", "devdocs_en_python_2026-10.zim", "other_2026-10.zim")
    books = corpus.list_books(tmp_path)
    assert [b.name for b in corpus.matching(books, "devdocs")] == [
        "devdocs_en_python_2026-10", "devdocs_en_rust_2026-10"]
    assert [b.name for b in corpus.matching(books, "RUST")] == ["devdocs_en_rust_2026-10"]
    assert [b.name for b in corpus.matching(books, "2026-10", "other")] == [
        "devdocs_en_python_2026-10", "devdocs_en_rust_2026-10", "other_2026-10"]


def test_the_cost_report_counts_bytes_and_names_the_big_books(tmp_path):
    """Cost is stated in bytes because bytes are what a listing measures; the big-book note says what
    follows from them (memory, for the server's whole life) without quoting a figure this function did
    not take."""
    make(tmp_path, "big_2026-10.zim", "small_2026-10.zim")
    big, small = (tmp_path / "big_2026-10.zim", tmp_path / "small_2026-10.zim")
    big.write_bytes(b"\0" * (51 * 1024 * 1024))   # 51 MB here; the threshold is relative to the test
    small.write_bytes(b"\0" * 1024)
    threshold, corpus.BIG_BOOK_BYTES = corpus.BIG_BOOK_BYTES, 50 * 1024 * 1024
    try:
        report = corpus.cost_report(corpus.list_books(tmp_path))
    finally:
        corpus.BIG_BOOK_BYTES = threshold
    assert report["count"] == 2 and report["big_books"] == ["big_2026-10"]
    assert "big_2026-10" in report["note"] and report["biggest"][0]["name"] == "big_2026-10"


def test_the_drift_line_compares_what_was_asked_to_what_is_served():
    """One line, with the remedy in it: the tool's configured set against the server's own catalog. Names
    compare lowercased, because they come from filenames on one side and a HTTP feed on the other."""
    drift = corpus.diff(["One_2026-10", "two_2026-10"], ["one_2026-10", "two_2026-10"])
    assert drift["same"] and (drift["configured"], drift["served"]) == (2, 2)
    apart = corpus.diff(["one", "two"], ["two", "three"])
    assert not apart["same"]
    assert apart["configured_only"] == ["one"] and apart["served_only"] == ["three"]


def test_the_command_line_length_is_measured_the_way_the_os_measures_it(tmp_path):
    """The ceiling that matters is on the assembled command line, so it is measured with the OS's own
    quoting rule rather than summed from path lengths — a corpus of paths with spaces is longer than the
    sum of its parts, and the failure it prevents happens at launch."""
    argv = ["kiwix-serve.exe", "-p", "18099", r"C:\a dir\one 2026-10.zim"]
    line = corpus.command_line(argv)
    # Quoting follows the platform; the fixture path does not (it is a Windows spelling either way).
    if os.name == "nt":
        assert '"C:\\a dir\\one 2026-10.zim"' in line
    else:
        assert "'C:\\a dir\\one 2026-10.zim'" in line
    assert len(line) > len(" ".join(argv))


def test_auto_launches_with_paths_until_the_command_line_grows(tmp_path, monkeypatch):
    """`auto` is the measured form (paths) until the assembled argv approaches the CreateProcess ceiling,
    and only then the library — the ceiling fails at launch, the least diagnosable place there is."""
    head = ["kiwix-serve.exe", "-p", "1", "-i", "127.0.0.1"]
    make(tmp_path, "one_2026-10.zim")
    short = corpus.list_books(tmp_path)
    assert corpus.launch_form_for(short, head + [b.path for b in short], "auto") == ("paths", len(
        corpus.command_line(head + [b.path for b in short])))
    long_head = head + ["x" * (corpus.COMMAND_LINE_CEILING + 10)]
    assert corpus.launch_form_for(short, long_head, "auto") == ("paths", len(corpus.command_line(
        long_head)))  # over the ceiling but the ids are unknown: paths stays, and the failure is explicit
    for book in short:
        book.zim_id, book.language = "00000000-0000-0000-0000-000000000000", "eng"
    assert corpus.launch_form_for(short, long_head, "auto")[0] == "library"
    with pytest.raises(ValueError):
        corpus.launch_form_for(short, head, "nonsense")


def test_an_explicit_form_is_honoured(tmp_path):
    """`paths` and `library` are stated acts; `auto` is the only one that measures anything."""
    head = ["kiwix-serve.exe", "-p", "1"]
    assert corpus.launch_form_for([], head, "paths")[0] == "paths"
    assert corpus.launch_form_for([], head, "library")[0] == "library"


def test_the_library_xml_refuses_to_invent_an_id(tmp_path):
    """A book entry whose id the engine cannot resolve makes every search against the library answer 500,
    so the generator refuses a book whose id or language it was not given rather than writing a plausible
    one. This is the whole difference between a config file and a made-up one."""
    books = corpus.list_books(make(tmp_path, "one_2026-10.zim"))
    with pytest.raises(ValueError) as caught:
        corpus.library_xml(books, tmp_path / "lib.xml")
    assert "id and language" in str(caught.value)
    assert not (tmp_path / "lib.xml").exists()


def test_the_library_xml_carries_absolute_paths_and_escapes(tmp_path):
    """Absolute paths, because the engine's working directory is not this tool's; the five entities,
    because a title with an ampersand is a library file that will not parse. `id`, `path` and `language`
    are the measured minimum of a searchable entry."""
    make(tmp_path, "one_2026-10.zim")
    book = corpus.list_books(tmp_path)[0]
    book.zim_id, book.language, book.title = "aaaa0000-0000-0000-0000-000000000001", "eng", "A & B <x>"
    target = tmp_path / "deep" / "lib.xml"
    corpus.library_xml([book], target)
    text = target.read_text(encoding="utf-8")
    assert 'id="aaaa0000-0000-0000-0000-000000000001"' in text
    assert 'language="eng"' in text and 'name="one_2026-10"' in text
    assert "A &amp; B &lt;x&gt;" in text
    assert str(tmp_path.resolve().drive) in text, "the path is absolute"


def test_catalog_entries_attach_by_the_content_link_not_the_catalog_name(tmp_path):
    """The engine's catalog pairs an id with a `/content/<name>` link, and that link is the file stem —
    its own `<name>` field is dateless, so matching on it would miss every book and the library form would
    then refuse to launch."""
    make(tmp_path, "devdocs_en_rust_2026-10.zim")
    book = corpus.list_books(tmp_path)[0]
    filled = corpus.attach_ids([book], [{"id": "beef-0", "name": "devdocs_en_rust", "language": "eng",
                                         "title": "Rust docs", "article_count": "10",
                                         "content_name": "devdocs_en_rust_2026-10"}])
    assert filled == 1
    assert (book.zim_id, book.language, book.title, book.article_count) == ("beef-0", "eng", "Rust docs", 10)


def test_the_catalog_renders_the_selection_it_is_holding(tmp_path):
    """The catalog states its own selection: a marker per picked book, so "which ones did I tick"
    is a line on the screen rather than a memory."""
    make(tmp_path, "one_2026-10.zim", "two_2026-10.zim")
    text = corpus.catalog_lines(corpus.list_books(tmp_path))
    assert text.count("one_2026-10") == 1 and "[*]" in text and "[one]" in text
    assert corpus.catalog_lines([]) == "  (none)"


def test_human_sizes_read_the_way_a_person_reads_them(tmp_path):
    """MB below a GiB, GB above, one decimal: the unit that makes a corpus listing scannable, and the same
    function in the console, the doctor and the catalog so a size never changes between screens."""
    assert corpus.human(500 * 1024) == "0 MB"
    assert corpus.human(1500 * 1024 * 1024) == "1.5 GB"
    assert corpus.human(2 * 1024 ** 3) == "2.0 GB"


# -- the header, pinned to bytes (M12) ------------------------------------------------

# The ZIM header of a real book -- 3dprinting.stackexchange.com_en_all_2026-10, first 80
# bytes read off disk -- as the one layout truth in this repo. `siw.zimmeta` reads it; the
# second reader this review found (uuid at offset 64, language at 60) answered noise with
# confidence and is deleted. The pin is the point: offsets in a binary format are exactly
# the kind of fact a test should hold.
ZIM_HEADER_HEX = (
    "5a494d0406000300374cedf951caf21cef43fc58a500d1791d8a0000bb000000"
    "53de270700000000ffffffffffffffff3b2f2c07000000005000000000000000"
    "198a0000ffffffff13352c0700000000")


def test_the_uuid_is_read_from_offset_8_measured_on_a_real_book(tmp_path):
    from siw import zimmeta
    head = bytes.fromhex(ZIM_HEADER_HEX)
    assert len(head) == 80
    zim = tmp_path / "book.zim"
    zim.write_bytes(head)
    got = zimmeta.read_header(zim)
    assert got is not None and got["uuid"] == "374cedf9-51ca-f21c-ef43-fc58a500d179"
    assert got["magic"] == "zim4"


def test_the_engine_id_fill_runs_through_that_reader(tmp_path):
    """`engine.ensure_ids` fills an unknown id from the book header when the cache does not
    know the book -- and there is now exactly one header reader in the repo to run through."""
    from siw import config as config_module
    from siw import engine as engine_mod
    zim = tmp_path / "book.zim"
    zim.write_bytes(bytes.fromhex(ZIM_HEADER_HEX))
    book = corpus.Book(zim)
    assert book.zim_id == ""
    cfg = config_module.Config({"corpus": str(tmp_path)}, path=tmp_path / "c.json")
    engine_mod.ensure_ids(cfg, [book])
    assert book.zim_id == "374cedf9-51ca-f21c-ef43-fc58a500d179"
    assert not hasattr(corpus, "read_header_zim_id")
