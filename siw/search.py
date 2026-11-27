"""siw.search -- the routes over the engine: the request, the feed parse, the answer record.

The engine answers a search with an RSS feed and nothing else: `format=json` returns the HTML template on
this build, so the feed is parsed here and the JSON the API serves is built here. Two routes exist, and
which one a query took is part of the answer, because the two cost different things and rank differently:

* **selection-less** — one request, `/search?pattern=<q>&pageLength=<n>`, with no book named. The engine
  merges the whole served set and ranks it as one, which is the best ordering available and the cheapest
  request to make. It costs reach: every served book is in scope, and a big book touched by a query is
  opened lazily and stays open.
* **`books.name`** — the selection passed explicitly, one parameter per book, chunked so the encoded
  request stays inside a byte budget. Narrower (the selection is only what the caller asked for), so the
  per-book cost is bounded, and the merge across chunks is done here — a round-robin of the per-chunk top
  lists, which is a display-order fix and not a ranking one, because the feed carries no cross-index
  score. It is the route a narrowed corpus or a server that refuses the first route ends up on.
* **per-book** — the anti-dominance route, asked for explicitly (`mode=grouped`): one request per book in
  the universe, each book putting forward its own best answer with its own hit count, the answers ordered
  by hit count. A single global ranking is the big index's ranking — a 4-million-article wikipedia drowns
  every other book's top hit — so a caller who wants the best few *across* the books, not the best few of
  wikipedia, asks this. Named `books=` get every one of their answers back (the caller chose the universe,
  so nothing in it is sliced); an un-named universe answers its top-`limit` books by hit count, the
  topic map. It costs one request per book, run concurrently under a small pool.

"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

from siw import corpus, engine

# The byte budget for one encoded request line. Measured on this build: a 30,000-character request line
# is accepted and a selection-less query answers in ~0.1 s, so the budget is not a workaround for a wall
# that is there — it is insurance against a build where it is, and a reason to make every chunk
# independently retryable. Sized on bytes because that is what a request line is measured in, and never
# on a hard-coded book count: filename length varies by corpus; budget in bytes, never in book counts.
REQUEST_BYTE_BUDGET = 6000

# The two zero-hit sentences, verbatim: they are part of the wire contract; callers branch on the
# wording. A scoped zero is a fact about the selection, a global zero is a fact about the corpus, and
# only the second one is worth concluding from.
ZERO_HIT_SCOPED = ("siw: no results for {query!r}{scope} — that means NOT IN THESE SOURCES, not 'not in "
                   "corpus': rerun without sources=/corpus= (global, all books) and/or loosen the query — "
                   "search is AND across all terms, so one rare token (e.g. a model number) kills the "
                   "whole query")
ZERO_HIT_GLOBAL = ("siw: no results for {query!r} across ALL sources (global route) — that is a "
                   "corpus-level zero: loosen the query (search is AND across all terms — drop the rarest "
                   "token) or rephrase; if it is a live-web fact the corpus may genuinely not carry it")

# A scope that names no book. It
# reads as a search result (zero hits) unless it says what actually happened, which is that nothing was
# searched because the filter matched nothing on this machine.
SCOPE_EMPTY = ("siw: the scope matched no configured book (sources={sources!r}, corpus={corpus!r}) so "
               "nothing was searched — an empty scope is NOT the whole corpus. Check the spelling against "
               "`GET /catalog` (the names are the file stems) or drop the filter to ask the whole corpus")

_TAG = re.compile(r"<[^>]+>")
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TOTAL = re.compile(r"totalResults[^>]*>([\d,]+)<")
_SEARCH_PATH = "/search?{query}"


class SearchError(Exception):
    """A search that could not be performed, with the code the API answers it with."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def encode(params: list[tuple[str, str]]) -> str:
    """A query string, encoded the way the engine wants it: repeated keys for a selection, no spaces."""
    return urllib.parse.urlencode(params)


def request_bytes(path: str, params: list[tuple[str, str]]) -> int:
    """The size of the request line this query would produce — the number the chunk budget is spent
    against, measured on the encoded form rather than estimated from the parameter count."""
    return len(("GET " + _SEARCH_PATH.format(query=encode(params)) + " HTTP/1.1").encode("utf-8"))


def chunk_names(names: list[str], head: list[tuple[str, str]],
                budget: int = REQUEST_BYTE_BUDGET) -> list[list[str]]:
    """Split a selection into requests, each one under the byte budget.

    The head (pattern, format, page length) is paid for in every request, so the budget is applied to the
    whole line, not to the selection alone — the difference between a chunk that fits and a 413 that
    reads as a server fault."""
    chunks: list[list[str]] = []
    current: list[str] = []
    for name in names:
        trial = current + [name]
        params = list(head) + [("books.name", n) for n in trial]
        if current and request_bytes("/search", params) > budget:
            chunks.append(current)
            current = [name]
        else:
            current = trial
    if current:
        chunks.append(current)
    return chunks


def decode_entities(text: str) -> str:
    """The feed's entities, numeric first and `&amp;` last.

    The order is the whole point: decoding `&amp;lt;` the other way round yields a `<` the book never
    held, and a snippet is quoted text — inventing markup in it is worse than leaving it escaped."""
    def numeric(match: re.Match) -> str:
        # Which alternative matched is which base: `&#x41;` and `&#65;` are the same character
        # written two ways.
        hex_part, decimal_part = match.group(1), match.group(2)
        try:
            return chr(int(hex_part, 16) if hex_part is not None else int(decimal_part))
        except (ValueError, OverflowError):
            return match.group(0)  # a number that is not a codepoint stays exactly as it was written

    text = re.sub(r"&#x([0-9a-fA-F]+);|&#(\d+);", numeric, text)
    return (text.replace("&quot;", '"').replace("&apos;", "'").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&amp;", "&"))


def plain(text: str) -> str:
    """Feed markup to quoted text: the `<b>` marks are the engine's highlighting, not content, and the
    whitespace it emits is layout."""
    return re.sub(r"\s+", " ", decode_entities(_TAG.sub(" ", text))).strip()


def parse_feed(body: str) -> tuple[int, list[dict]]:
    """The index total and the items: title, book, URL, snippet.

    `totalResults` arrives comma-grouped ("1,000"), so the commas come off before it becomes data; the
    book a result came from is read from its own `/content/<name>` link, because that is the name the
    selector speaks and the one a caller can pass back."""
    total = re.search(_TOTAL, body)
    items = []
    for block in _ITEM.findall(body):
        def field(tag: str, raw: bool = False) -> str:
            found = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
            if not found:
                return ""
            return found.group(1).strip() if raw else plain(found.group(1))
        link = field("link", raw=True)
        book = re.match(r"/content/([^/]+)/", link)
        items.append({"title": field("title"),
                      "book": book.group(1) if book else (field("book") or "?"),
                      "url": f"/{link.lstrip('/')}" if link else "",
                      "snippet": field("description")})
    raw_total = (total.group(1) if total else "0").strip()
    shape = r"\d{1,3}(,\d{3})*" if "," in raw_total else r"\d+"
    if not re.fullmatch(shape, raw_total):
        # "1,0,0" is not a comma-grouped number; totaling it silently turns a broken
        # feed into a confident count. The route's error path is the honest answer.
        raise ValueError(f"the feed total {raw_total!r} is not a number")
    return int(raw_total.replace(",", "")), items


def scope(config, books: list[corpus.Book], served: int | None = None) -> dict:
    """The record's own account of what it searched: the directory, the pick, how many books are in scope,
    and how many the server says it serves."""
    return {"corpus": config.corpus, "configured": len(books),
            "selected": config.selected_books(), "served": served}


def resolve_sources(books: list[corpus.Book], sources: str) -> list[str]:
    """The `sources=` terms resolved against the configured set, to the names the engine selects.

    Resolution is client-side and deliberate: the engine's selection is exact and all-or-nothing, so one
    name it does not hold 400s the whole request. Matching here against the listing the tool just read
    turns a typo into "no book matched" — a legible answer — and leaves the engine free to validate the
    names it is given."""
    names: list[str] = []
    for term in re.split(r"[\s,;]+", sources or ""):
        if not term:
            continue
        for book in corpus.matching(books, term):
            if book.name not in names:
                names.append(book.name)
    return names


def search(config, query: str, limit: int = 5, sources: str = "", corpus_filter: str = "",
           books: list[corpus.Book] | None = None, heal: bool = True,
           timeout: float = 180.0, mode: str = "global") -> dict:
    """The one entry point: a query in, an answer record out.

    `books` is the set the caller wants in scope — usually the configured pick, and the argument a test
    or a narrowed caller supplies. `heal` follows the engine's own rule: an endpoint that is dark is
    started if SIW is allowed to start it, and the record reports the heal either way.

    `mode="grouped"` switches to the per-book route (`search_grouped`): the universe is `books` when the
    caller supplies it, else the configured scope, and `sources`/`corpus` are refused with it because
    they are selection spellings for the global path."""
    words = (query or "").strip()
    if not words:
        raise SearchError("empty_query", "nothing to search for: the query is empty", 400)
    if (mode or "global").strip().lower() == "grouped":
        if (sources or "").strip() or (corpus_filter or "").strip():
            raise SearchError("bad_mode", "mode=grouped takes its universe from `books` (or the whole "
                                           "scope); `sources`/`corpus` are selection spellings for the "
                                           "global path", 400)
        # The global route's timeout is for one request; grouped asks many. It takes a
        # per-book cap and a wall-clock route deadline rather than inheriting `timeout`
        # once per book.
        return search_grouped(config, words, limit=limit, books=books, heal=heal,
                              timeout=min(float(timeout), 30.0), route_budget=60.0)
    n = max(1, min(5 if limit is None else int(limit), 25))
    scope_books = books if books is not None else _configured(config)
    started = time.monotonic()
    healed_seconds, heal_note = (None, "")
    if heal:
        up, seconds, note = engine.heal(config, scope_books)
        if not up:
            raise SearchError("engine_down", (note or f"{config.host}:{config.port} is not answering"), 503)
        if seconds:
            healed_seconds, heal_note = seconds, note

    selection = resolve_sources(scope_books, sources) if sources else []
    if corpus_filter:
        narrowed = [b.name for b in corpus.matching(scope_books, corpus_filter)]
        selection = [n for n in selection if n in narrowed] if selection else narrowed
    if (sources or corpus_filter) and not selection:
        # A scope that resolves to nothing is answered as nothing, and never as the whole corpus: the
        # caller asked a narrower question, and answering the wide one instead turns a typo into a
        # confident wrong answer. This is the same rule the zero-hit sentences carry, applied one step
        # earlier — before the engine is asked at all.
        return _record(config, words, "books.name",
                       scope(config, scope_books, served=_served_count(config)), [], 0, n,
                       healed_seconds, heal_note, started,
                       zero_hit=SCOPE_EMPTY.format(sources=sources, corpus=corpus_filter))
    # `healed_seconds` is None when heal was refused: nothing was just launched then.
    fresh = healed_seconds is not None and 0 < healed_seconds < 10.0
    if selection:
        items, total = _fanout(config, words, selection, n, timeout, fresh_launch=fresh)
        route = "books.name"
    else:
        items, total, route = _selectionless(config, words, n, scope_books, timeout,
                                             fresh_launch=fresh)

    if corpus_filter:
        items = [i for i in items if corpus_filter.lower() in str(i.get("book", "")).lower()]
    items = items[:n]
    zero = ""
    if not items:
        if selection:
            named = ", ".join(selection[:6]) + ("…" if len(selection) > 6 else "")
            zero = ZERO_HIT_SCOPED.format(query=words, scope=f" in {named}")
        else:
            zero = ZERO_HIT_GLOBAL.format(query=words)
    return _record(config, words, route, scope(config, scope_books, served=_served_count(config)),
                   items, total, n, healed_seconds, heal_note, started, zero_hit=zero)


def search_grouped(config, query: str, limit: int = 10, books: list[corpus.Book] | None = None,
                   heal: bool = True, timeout: float = 30.0, workers: int = 8,
                   route_budget: float = 60.0) -> dict:
    """The per-book answer to the dominance problem.

    One global ranking is the big index's ranking: with a 4-million-article wikipedia in scope, every
    other book's top hit sits below wikipedia's millionth and never surfaces, relevant or not. So each
    book in the universe is asked the question alone and puts forward its own best answer, with the hit
    count that book got — each book reports its own hit count — and the caller takes the best few of
    the books rather than the best few of one book.

    The universe is `books` when the caller supplies it, else the configured scope. Two shapes follow
    from that: a *named* universe answers every book it holds — the caller chose the set, so nothing in
    it is sliced and `limit` does not cut it (that is the "best few of all the books I named" call) —
    while an un-named universe answers its top-`limit` books by hit count, the topic map. Answers are
    ordered by per-book hit count, most-hit first: a book that holds many hits for the query is the book
    the query is about, and the hit count is the only cross-book signal the feed carries, so it is the
    only honest ordering available (a true cross-book relevance ranking is not computable from this
    feed). Books with
    zero hits stay out of the results (they have no answer to put forward) and are counted instead; a
    book the engine does not serve is counted as an error, and the record says which. The per-book
    requests run concurrently under a small pool because they are independent and the engine is a
    server; a large corpus has measured answering in under two seconds.
    """
    words = (query or "").strip()
    if not words:
        raise SearchError("empty_query", "nothing to search for: the query is empty", 400)
    n = max(1, min(10 if limit is None else int(limit), 25))
    universe = books if books is not None else _configured(config)
    if not universe:
        raise SearchError("empty_scope",
                          "grouped search has no books: the configured scope is empty — point the "
                          "corpus somewhere or pass books=", 400)
    started = time.monotonic()
    healed_seconds, heal_note = (None, "")
    if heal:
        up, seconds, note = engine.heal(config, universe)
        if not up:
            raise SearchError("engine_down", (note or f"{config.host}:{config.port} is not answering"), 503)
        if seconds:
            healed_seconds, heal_note = seconds, note

    head = [("pattern", words), ("format", "xml"), ("pageLength", "1")]

    deadline = time.monotonic() + route_budget

    def ask(book) -> dict:
        params = head + [("books.name", book.name)]
        left = deadline - time.monotonic()
        if left <= 0:
            return {"book": book.name,
                    "error": f"{book.name}: the grouped route ran its {route_budget:.0f}s "
                             "deadline before this book was asked"}
        try:
            status, body = engine.get(config, "/search?" + encode(params),
                                      timeout=min(timeout, max(1.0, left)))
        except (OSError, ValueError) as exc:
            # One book's socket error is that book's error, recorded and passed over --
            # it must not take the answers already collected down with it. (URLError and
            # the http failures are OSError subclasses; parse failures are ValueError.)
            return {"book": book.name,
                    "error": f"{book.name}: {exc.__class__.__name__}: {exc}"}
        if status != 200 or not body.lstrip().startswith("<"):
            return {"book": book.name, "error": f"the engine answered HTTP {status} to {book.name}"}
        try:
            total, items = parse_feed(body)
        except ValueError as exc:
            return {"book": book.name, "error": f"{book.name}: the feed did not parse ({exc})"}
        if not items:
            return {"book": book.name, "zero": True}
        item = dict(items[0])
        item["hits_in_book"] = total
        return {"book": book.name, "total": total, "item": item}

    answers = []
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(universe))))
    try:
        futures = {pool.submit(ask, book): book for book in universe}
        done, _pending = futures_wait(list(futures), timeout=route_budget)
        for fut, book in futures.items():
            if fut in done:
                try:
                    answers.append(fut.result())
                except Exception as exc:  # ask() catches; this is the belt for what it cannot
                    answers.append({"book": book.name,
                                    "error": f"{book.name}: {exc.__class__.__name__}"})
            else:
                fut.cancel()
                answers.append({"book": book.name,
                                "error": f"{book.name}: the grouped route ran its "
                                         f"{route_budget:.0f}s deadline before this book answered"})
    finally:
        pool.shutdown(wait=False)

    hits = sorted((a for a in answers if "item" in a), key=lambda a: a["total"], reverse=True)
    named = books is not None
    items = [a["item"] for a in (hits if named else hits[:n])]
    total = sum(a.get("total", 0) for a in answers)
    zero = ""
    if not items:
        zero = (f"siw: no book in the scope had a single hit for {words!r} ({len(universe)} books asked "
                "per-book) — a corpus-level zero: loosen the query (search is AND across all terms) or "
                "rephrase; if it is a live-web fact the corpus may genuinely not carry it")
    record = _record(config, words, "grouped (per-book)",
                     scope(config, universe, served=_served_count(config)),
                     items, total, len(items) if named else n, healed_seconds, heal_note, started,
                     zero_hit=zero)
    record["books_asked"] = len(universe)
    record["books_zero"] = sum(1 for a in answers if a.get("zero"))
    record["book_errors"] = [a["error"] for a in answers if a.get("error")]
    return record


def _configured(config) -> list[corpus.Book]:
    """The configured pick against the directory as it is now — the pick, not the whole folder."""
    return corpus.select(corpus.list_books(config.corpus), config.selected_books())


def _selectionless(config, words: str, n: int, books: list[corpus.Book],
                   timeout: float, fresh_launch: bool = False) -> tuple[list[dict], int, str]:
    """One request, no selection: the engine's own merged ranking over everything it serves.

    A 400/500 here is the trigger for the explicit-selection route: on the measured build it means a
    server that will not answer a selection-less query (older builds, or a `--searchLimit` set), and the
    fan-out is the same question asked in a form that works. A connection-level failure is not retried
    here — the heal has already run.

    It returns the route that actually answered, `selection-less` or `books.name (fallback)`, because the
    record is what makes the answer reproducible: "which request produced this" is a question a reader of
    the record has to be able to trust: the record names the request that actually answered.
    Note the order: (items, total, route), unlike `parse_feed`'s (total, items).
    """
    head = [("pattern", words), ("format", "xml"), ("pageLength", str(n))]
    status, body = engine.get(config, "/search?" + encode(head), timeout=timeout)
    if status == 200:
        items, total = _feed(body)
        return items, total, "selection-less"
    if status in (400, 500):
        if fresh_launch:
            # A first touch right after a launch can race (measured: a 400 that 200s on
            # re-run); only that window pays the 2 s before believing the rejection.
            time.sleep(2.0)
            status, body = engine.get(config, "/search?" + encode(head), timeout=timeout)
            if status == 200:
                items, total = _feed(body)
                return items, total, "selection-less"
        # A 400 here is the documented trigger for the explicit-selection route either
        # way: an older build, or a `--searchLimit` set. That fallback is not the retry
        # -- it is the answer to a different question ("this server will not take the
        # selection-less form") and it runs whether or not the launch was fresh.
        names = [b.name for b in books]
        if names:
            items, total = _fanout(config, words, names, n, timeout, fresh_launch=fresh_launch)
            return items, total, "books.name (fallback)"
    raise SearchError("engine_rejected", f"the engine answered HTTP {status} to a search: {body[:200]}",
                      502)


def _feed(body: str) -> tuple[list[dict], int]:
    """`parse_feed` with its two answers swapped into the order the routes return: items, then total."""
    total, items = parse_feed(body)
    return items, total


def _fanout(config, words: str, names: list[str], n: int, timeout: float,
            fresh_launch: bool = False) -> tuple[list[dict], int]:
    """The explicit-selection route: the set chunked under the request budget, the feeds merged here.

    Merging is a round-robin of the per-chunk top lists. The engine exposes no cross-index score, so this
    is a display-order fix rather than a ranking one — without it a rank-1 result in the second chunk
    would appear below the whole first chunk's list, and a general question's best answer could sit under
    a hundred and thirty hits."""
    head = [("pattern", words), ("format", "xml"), ("pageLength", str(n))]
    lists: list[list[dict]] = []
    total = 0
    for chunk in chunk_names(names, head, REQUEST_BYTE_BUDGET):
        params = list(head) + [("books.name", name) for name in chunk]
        status, body = engine.get(config, "/search?" + encode(params), timeout=timeout)
        if status in (400, 500) and fresh_launch:
            # Same rule as the selection-less route: re-ask once only in the window
            # where a 400 can still mean "the server is on its way up".
            time.sleep(2.0)
            status, body = engine.get(config, "/search?" + encode(params), timeout=timeout)
        if status != 200:
            if status == 400 and "No such book" in body:
                raise SearchError("unknown_book", (f"the engine does not know one of the selected books "
                                                   f"({chunk[0]}…): it was added or removed since the "
                                                   "server loaded. Restart the engine (siw serve) or "
                                                   "check the catalog (`GET /catalog`)"), 409)
            raise SearchError("engine_rejected", f"the engine answered HTTP {status}: {body[:200]}", 502)
        count, items = parse_feed(body)
        total += count
        lists.append(items)
    merged: list[dict] = []
    row = 0
    while len(merged) < n:
        added = False
        for items in lists:
            if row < len(items):
                merged.append(items[row])
                added = True
                if len(merged) >= n:
                    break
        if not added:
            break
        row += 1
    return merged, total


def _served_count(config) -> int | None:
    """How many books the running engine says it serves — the number the drift check reads. A catalog
    that cannot be reached is `None`, not zero: an unknown served count and an empty server are different
    statements and must not print the same."""
    entries, problem = engine.catalog(config, timeout=10.0)
    return None if problem else len(entries)


def _record(config, words: str, route: str, scope_: dict, items: list[dict], total: int, asked: int,
            healed_seconds: float | None, heal_note: str, started: float,
            zero_hit: str = "") -> dict:
    """The answer record, in the order an adapter prints it. Every field is a fact about the request that
    just happened, and the shape is the contract `docs/api.json` describes."""
    return {"query": words, "route": route, "scope": scope_, "total": total, "count": len(items),
            "asked": asked, "results": items, "healed_in_seconds": healed_seconds,
            "heal_note": heal_note, "seconds": round(time.monotonic() - started, 3),
            "zero_hit": zero_hit, "endpoint": f"http://{config.host}:{config.port}"}
