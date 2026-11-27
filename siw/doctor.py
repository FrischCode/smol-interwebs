"""siw.doctor -- one command that answers "is this deployment healthy, and if not, what do I change?".

The exit code is the product for a script and the printed block is the product for a person, and each
answer has exactly one code so a boot check can branch without reading English:

    0 healthy   1 config   2 corpus   3 binary   4 engine down   5 drift   6 smoke query

The order is the dependency chain, and it short-circuits: corpus facts mean nothing if the config did not
load, drift means nothing if the engine is not answering, and a smoke query means nothing below any of
them. Reporting ten findings when the first one explains the other nine is how a diagnostic becomes noise.

Two things this deliberately does **not** do: it does not start the engine (`siw serve` is the verb that
starts things), and it does not read a book. The corpus is listed, never opened — with the exception of the 80-byte header read
behind `--library-ids`, which is the same read the engine performs to recognise a file.

`5 drift` is the code with the most value per line: it is the difference between "the pick is not working"
and "the pick is not working because the server that answers this port loaded a different set", and when the
engine was started outside SIW it is the only way to see that the two disagree.
"""
from __future__ import annotations

from pathlib import Path

from siw import api
from siw import config as config_module
from siw import corpus, engine, search

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_CORPUS = 2
EXIT_BINARY = 3
EXIT_ENGINE_DOWN = 4
EXIT_DRIFT = 5
EXIT_SMOKE = 6

EXIT_NAMES = {EXIT_OK: "healthy", EXIT_CONFIG: "config", EXIT_CORPUS: "corpus", EXIT_BINARY: "binary",
              EXIT_ENGINE_DOWN: "engine down", EXIT_DRIFT: "drift", EXIT_SMOKE: "smoke query"}

# The smoke query, chosen because it is short, present in almost any corpus, and answerable without a
# book being opened in full. It is a liveness probe of the search path, not an evaluation of it.
SMOKE_QUERY = "photosynthesis"


def report(config, smoke_query: str = "", library_ids: bool = False) -> tuple[int, list[str]]:
    """Every check, in dependency order, as (exit code, lines). Pure enough to test: it starts nothing and
    reads only listings, the pid record, and the running engine's own answers."""
    lines: list[str] = []
    lines.append(f"siw doctor on {config_module.platform_note()}")
    lines.append(f"  config: {config.path or '<defaults: no file read>'}")
    for name in ("corpus", "selected", "port", "api_port", "host", "bind", "exe", "launch"):
        value = config.values[name]
        shown = value if not isinstance(value, list) else f"{len(value)} books"
        lines.append(f"    {name} = {shown!r} ({config.sources[name]})")
    if config.path is None:
        lines.append("  finding: no config file was read, so every value above is a default. The "
                     "console's Settings pane (`siw serve`, then open the console URL) writes one")

    books = corpus.list_books(config.corpus)
    code = _corpus_checks(config, books, lines)
    if code:
        return code, lines

    code = _binary_checks(config, lines)
    if code:
        return code, lines

    picked = corpus.select(books, config.selected_books())
    up = engine.probe(config.host, config.port)
    bound_on = engine.listening_on(int(config.port))
    lines.append(f"  engine {config.host}:{config.port}: {'answering' if up else 'NOT ANSWERING'}")
    lines.append(f"    bound on: {', '.join(bound_on) or '<nothing listening>'}"
                 f" — loopback only: {_loopback(bound_on)}")
    if _loopback(bound_on) is False:
        lines.append("  note: this engine answers on every interface, with no authentication on it. If "
                     "that is not the plan, it needs a bind of 127.0.0.1 (`siw serve` passes -i itself).")
    record, where = engine.read_pidfile(config)
    if record:
        lines.append(f"    started by siw: pid {record.get('pid')} form {record.get('form')} "
                     f"{record.get('books')} books (record {where})")
    else:
        lines.append(f"    started by siw: no (no record at {where})")
    log = engine.log_path(config)
    lines.append(f"    engine log: {log}{'' if log.is_file() else ' (not written yet)'}")
    if not up:
        lines.append(f"  finding: nothing answers {config.host}:{config.port}. Start it with "
                     f"`siw serve` (it launches the engine if it is needed); if the engine is run by "
                     "another supervisor, check that port and `host` match what it actually bound")
        return EXIT_ENGINE_DOWN, lines

    entries, problem = engine.catalog(config, timeout=15.0)
    if problem:
        lines.append(f"  finding: {problem} — the port answers but the catalog does not, so served-vs-"
                     "configured cannot be checked")
        return EXIT_ENGINE_DOWN, lines
    served_names = [entry["content_name"] for entry in entries if entry["content_name"]]
    filled = corpus.attach_ids(books, entries)
    lines.append(f"  catalog: {len(entries)} entries, ids filled on {filled} of {len(books)} books on disk")
    if library_ids:
        cached = corpus.save_id_cache(books, engine.id_cache_path(config))
        lines.append(f"  ids: {filled} cached at {cached} — the library launch form reads them back with "
                     "the engine down")
    drift = corpus.diff([book.name for book in picked], served_names)
    lines.append(f"  served vs configured: {drift['served']} served / {drift['configured']} configured, "
                 f"{'same set' if drift['same'] else 'DRIFT'}")
    for label, key in (("on disk but not served", "configured_only"),
                       ("served but not configured", "served_only")):
        if drift[key]:
            shown = ", ".join(drift[key][:6]) + ("…" if len(drift[key]) > 6 else "")
            lines.append(f"    {label}: {len(drift[key])} — {shown}")
    if not drift["same"]:
        lines.append("  finding: the server on this port loaded a different set than the config picks. "
                     "`siw stop` and `siw serve` to relaunch on the pick, or accept the other set and "
                     "point the config at that server")
        return EXIT_DRIFT, lines

    if smoke_query:
        code, smoke_lines = _smoke(config, picked, smoke_query)
        lines.extend(smoke_lines)
        if code:
            return code, lines
    return EXIT_OK, lines


def _corpus_checks(config, books: list[corpus.Book], lines: list[str]) -> int:
    """The directory exists, holds at least one complete book, and the pick resolves to books."""
    if not config.corpus:
        lines.append("  finding: no corpus directory is set. Pick it in the console's Settings pane "
                     "(`siw serve`, then open the console URL)")
        return EXIT_CORPUS
    if not Path(config.corpus).is_dir():
        lines.append(f"  finding: the corpus path is not a directory: {config.corpus}")
        return EXIT_CORPUS
    partial = corpus.parts(config.corpus)
    lines.append(f"  corpus: {config.corpus} — {len(books)} complete books "
                 f"({corpus.human(sum(book.size for book in books))})")
    if partial:
        lines.append(f"    partial downloads seen and excluded: {len(partial)} — {', '.join(partial[:4])}")
    if not books:
        lines.append("  finding: no complete .zim file in that directory (a .zim.part set is an "
                     "unfinished download and is never served)")
        return EXIT_CORPUS
    unknown = corpus.unknown_names(books, config.selected_books())
    if unknown:
        shown = ", ".join(unknown[:6]) + ("…" if len(unknown) > 6 else "")
        lines.append(f"  finding: the pick names {len(unknown)} book(s) not on disk: {shown}. They are "
                     "excluded from the launch; the console lists every file in the folder, and "
                     "`GET /catalog` what the engine serves")
        return EXIT_CORPUS
    if not corpus.select(books, config.selected_books()):
        lines.append("  finding: the pick matches nothing in this directory, so there is nothing to serve")
        return EXIT_CORPUS
    return EXIT_OK


def _binary_checks(config, lines: list[str]) -> int:
    """The engine binary the launch would use, found and answering `--version`."""
    if not config.exe:
        lines.append("  finding: no kiwix-serve binary is set and none was found beside the corpus. "
                     "point `exe` at one in the console's Settings pane, or clear the field and let "
                     "the pinned fetch download it")
        return EXIT_BINARY
    text, problem = engine.version(config.exe)
    lines.append(f"  binary: {config.exe}")
    if problem:
        lines.append(f"  finding: {problem}. The launch would fail here, so nothing else was checked")
        return EXIT_BINARY
    lines.append(f"    version: {text}")
    supported, note = engine.platform_support()
    lines.append(f"    platform: {note}")
    if not supported:
        lines.append("    note: this is the first run of the detached spawn on this platform — the "
                     "Windows branch is the measured one")
    return EXIT_OK


def _smoke(config, picked: list[corpus.Book], words: str) -> tuple[int, list[str]]:
    """One real query through the same route `siw doctor --query WORD` takes, `heal=False` so a check cannot start a
    server. A zero-hit answer is *not* a failure: an empty result is a working search over a corpus that
    does not hold the word."""
    try:
        record = search.search(config, words, limit=1, books=picked, heal=False)
    except (search.SearchError, engine.EngineError, OSError) as exc:
        return EXIT_SMOKE, [f"  finding: the smoke query failed: {exc}"]
    line = (f"  smoke: {words!r} → route {record['route']}, {record['count']} of {record['total']} "
            f"index hits in {record['seconds']}s")
    if record["results"]:
        return EXIT_OK, [line, f"    first: {record['results'][0]['title']} "
                               f"[{record['results'][0]['book']}]"]
    return EXIT_OK, [line, "    zero hits, which is a working search over a corpus without that word"]


def _loopback(bound_on: list[str]):
    """Is every address on this port a loopback one? The one predicate for the whole tool,
    living with the kernel (`api.loopback_only`): None when the listing could not say so — an
    unknown bind must not read as a safe one, which is the whole difference between this
    check and an assumption."""
    return api.loopback_only(bound_on)
