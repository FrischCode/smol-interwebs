"""siw.corpus -- the corpus directory as facts: the books on disk, the picked subset, what is served.

The corpus is the operator's, never the tool's: SIW lists a directory and asks about it, and never
downloads, writes, renames or repairs a ZIM. Everything here is a read of a directory listing or a
pure function over names and sizes — no HTTP (that is `siw.engine`), no process, no mutation.

Two names for a book matter and are kept apart deliberately:

* the **book name** is the file stem, `devdocs_en_rust_2026-10`. It is what `books.name=` selects and
  what the engine puts in a `/content/<name>` URL, so it is the name a launch and a search speak.
* the **short name** is the same stem with the date and the edition tag off, `devdocs_rust`. It is what
  a person types to narrow a search, and it keeps the language because picking the Rust docs for the
  wrong language is a wrong answer.

A file is a book only when it is a complete `*.zim`: an in-flight `*.zim.part` download is a file the
engine would open and pay a whole cold load for, and one that disappears mid-startup has hung this
engine family at launch rather than failing it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# The CreateProcess command-line ceiling. Every picked book pays its full absolute path, so a set
# in the hundreds reaches it — and it fails at launch, the least diagnosable moment available.
COMMAND_LINE_CEILING = 32767

# A book big enough that a selection-less search touching it grows the engine's memory by hundreds of
# megabytes for the rest of its life. The threshold here only decides when to *say* something, and says it in bytes, which are measured.
BIG_BOOK_BYTES = 50 * 1024 ** 3

_ZIM_PART = re.compile(r"\.zim\.part\d*$", re.I)
_DATE = re.compile(r"_\d{4}-\d{2}(-\d{2})?$")
# Everything an edition tag can be, so a short name is the *subject* of the book: the Kiwix filenames put
# the language and the flavour between the subject and the date (`wikipedia_en_all_maxi_2026-08`,
# `devdocs_en_rust_2026-10`), and the two must be told apart — dropping `rust` would collapse six books
# into one, keeping `maxi` would make a typed `wikipedia` match nothing.
_EDITION_TOKENS = frozenset({"all", "maxi", "mini", "nopic", "noprev", "top100", "top1m", "static",
                             "simple", "novid", "vids", "part"})


class Book:
    """One book on disk: its path, the two names, and its size.

    `zim_id` and `language` start empty and are filled from the running engine's catalog, or from the
    80-byte ZIM header when no engine is up — both are needed only by the library launch form, which
    refuses to invent them (an id the engine does not recognise 500s every search against the library)."""

    def __init__(self, path: str | Path, zim_id: str = "", language: str = "",
                 title: str = "", article_count: int | None = None):
        self.path = str(path)
        self.name = Path(self.path).stem
        self.short_name = short_name(self.name)
        self.size = self._size()
        self.zim_id = zim_id
        self.language = language
        self.title = title
        self.article_count = article_count

    def _size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0  # raced away between listing and stat; a size of 0 reads as "unknown" downstream

    def __repr__(self) -> str:
        return f"Book({self.name}, {self.size // 1048576} MB)"

    def as_dict(self) -> dict:
        return {"name": self.name, "short_name": self.short_name, "path": self.path,
                "size_bytes": self.size, "id": self.zim_id, "language": self.language,
                "title": self.title, "articles": self.article_count}


def short_name(name: str) -> str:
    """The book's subject: date off, then the language tag and any edition tokens after it off.

    `devdocs_en_rust_2026-10` -> `devdocs_rust` (the language is kept where it is the subject — six
    devdocs books differ only by it), `wikipedia_en_all_maxi_2026-08` -> `wikipedia`, and
    `askubuntu.com_en_all_2026-10` -> `askubuntu.com`. Tokens are stripped in order from the language tag
    and stop at the first word that is not an edition, so a subject that happens to end in a dated tag is
    not chopped."""
    stem = _DATE.sub("", name)
    parts = stem.split("_")
    if "en" not in parts:
        return stem
    cut = parts.index("en")
    tail = parts[cut + 1:]
    keep = 0
    while keep < len(tail) and tail[keep].lower() in _EDITION_TOKENS:
        keep += 1
    rest = tail[keep:]
    return "_".join(parts[:cut] + rest) if rest else "_".join(parts[:cut])


def list_books(corpus: str | Path | None) -> list[Book]:
    """Every complete book in the directory, by name — or an empty list when the directory is not there.

    Sorting is not cosmetic: a picked subset is a list of names in a file, and a listing that drifts
    between runs makes a saved pick look like it changed when only the filesystem's order did."""
    if not corpus:
        return []
    folder = Path(corpus).expanduser()
    if not folder.is_dir():
        return []
    books = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file() or _ZIM_PART.search(entry.name):
            continue
        if entry.suffix.lower() != ".zim":
            continue
        books.append(Book(entry))
    return books


def parts(corpus: str | Path | None) -> list[str]:
    """In-flight downloads seen and skipped — the reason a launch excludes them is stated once, here.

    Counted rather than hidden because `siw doctor` prints it: a corpus mid-download is a corpus that is
    not what it looks like."""
    if not corpus:
        return []
    folder = Path(corpus).expanduser()
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and _ZIM_PART.search(p.name))


def select(books: list[Book], names: list[str] | None) -> list[Book]:
    """The picked subset, in the order the pick lists it, unknown names dropped from the result.

    `names=None` is the whole corpus: the tool never narrows a corpus nobody narrowed. An empty name
    list is *not* the same thing and is refused the same way by keeping every book — a config that lost
    its selection must not silently become a search over nothing."""
    if not names:
        return list(books)
    by_name = {book.name.lower(): book for book in books}
    by_short = {}
    for book in books:
        by_short.setdefault(book.short_name.lower(), book)
    out = []
    for want in names:
        key = str(want).strip().lower()
        book = by_name.get(key) or by_short.get(key)
        if book is not None and book not in out:
            out.append(book)
    return out


def unknown_names(books: list[Book], names: list[str] | None) -> list[str]:
    """Names a pick asks for that the directory does not hold — a stale pick, reported not guessed at."""
    if not names:
        return []
    known = {book.name.lower() for book in books} | {book.short_name.lower() for book in books}
    return [str(want) for want in names if str(want).strip().lower() not in known]


def matching(books: list[Book], *terms: str) -> list[Book]:
    """Books whose short name or full name contains any term, case-insensitively — the Settings pane's filter
    and the API's `sources=` selector are the same function, so a word means the same thing in both."""
    out = []
    lowered = [t.lower() for t in terms if t]
    if not lowered:
        return list(books)
    for book in books:
        if any(t in book.short_name.lower() or t in book.name.lower() for t in lowered):
            out.append(book)
    return out


def catalog_lines(books: list[Book], mark: str = "*", picked: list[str] | None = None) -> str:
    """The catalog as text: `[*] name  <size>  [short]` — what the catalog route renders. The marker column is the selection, so the screen states which books are in the pick instead
    of letting the operator remember what was ticked; `picked=None` means every book is marked, which is
    what an unpicked corpus is."""
    wanted = None if picked is None else {str(name).lower() for name in picked}
    lines = []
    for book in books:
        on = wanted is None or book.name.lower() in wanted or book.short_name.lower() in wanted
        lines.append(f"  [{'*' if on else ' '}] {book.name}  {human(book.size)}  [{book.short_name}]")
    return "\n".join(lines) or "  (none)"


def human(nbytes: int) -> str:
    """Bytes in the unit a person reads: MB below a GiB, GB above, one decimal."""
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024 ** 3:.1f} GB"
    return f"{nbytes // 1048576} MB"


def cost_report(books: list[Book]) -> dict:
    """What this set costs to serve: count, bytes, the biggest names, and the big-book note.

    The note is not a scare: a selection-less query opens the big books lazily and the engine keeps the
    index pages it reads. It says bytes (measured here) and that memory follows (concluded), and never
    quotes an RSS figure it did not take."""
    total = sum(book.size for book in books)
    biggest = sorted(books, key=lambda b: -b.size)[:3]
    big = [book.name for book in books if book.size >= BIG_BOOK_BYTES]
    note = ""
    if big:
        note = (f"{len(big)} book(s) over {human(BIG_BOOK_BYTES)}: {', '.join(big)} — a selection-less "
                "search opens them lazily and the engine keeps what it reads, so a query that touches "
                "them costs memory for the server's whole life; the narrowed routes cost less of it")
    return {"count": len(books), "bytes": total, "size": human(total),
            "biggest": [{"name": b.name, "size_bytes": b.size} for b in biggest],
            "big_books": big, "note": note}


def diff(configured: list[str], served: list[str]) -> dict:
    """Configured set vs served set — the drift check that turns "my subset is not my subset" into one
    readable line, with the remedy in it.

    Names are compared lowercased: they are compared against a server that got them from filenames, and
    a case difference is not a different corpus. `same` is what a caller asserts on; the lists are what
    it prints."""
    want = {str(n).lower() for n in configured if str(n).strip()}
    got = {str(n).lower() for n in served if str(n).strip()}
    return {"configured": len(want), "served": len(got), "same": want == got,
            "configured_only": sorted(want - got), "served_only": sorted(got - want)}


def command_line(argv: list[str]) -> str:
    """The command line the spawn will actually produce, measured the way the OS measures it.

    Windows quotes as `list2cmdline` does (a path with a space keeps its backslash and gains quotes);
    POSIX quoting is shell-level, so `shlex.join` is the honest spelling. The length is what the launch
    ceiling applies to, so it is computed on the real argv rather than estimated from a book count."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex
    return shlex.join(argv)


def launch_form_for(books: list[Book], argv_head: list[str], want: str = "auto") -> tuple[str, str]:
    """Which launch form to use, and the command-line length that decided it.

    `paths` names every ZIM on the command line — the form measured working on this engine family, and the
    one that needs nothing extra. `library` passes one XML path instead, which is the only form that
    survives a large picked set, and it costs real book ids (see `library_xml`). `auto` is `paths` until
    the command line would come within a sixth of the ceiling, then `library` — the ceiling fails at
    launch, which is the worst possible place to discover a corpus is too big."""
    length = len(command_line(argv_head))
    if want in ("paths", "library"):
        return want, length
    if want != "auto":
        raise ValueError(f"unknown launch form {want!r} (wants auto, paths or library)")
    if length > COMMAND_LINE_CEILING * 5 // 6 and all(book.zim_id and book.language for book in books):
        return "library", length
    return "paths", length


def library_xml(books: list[Book], path: str | Path) -> Path:
    """Write the library XML the `--library` launch form takes, and return its path.

    `id`, `path` and `language` are mandatory in practice: measured on the x64 3.8.1 build, a book entry
    with no `language` makes `/search` answer 400 and an id the file does not carry makes it answer 500,
    while `id` + absolute `path` + `language` serves and searches — so `name`/`title` are written only when
    they are known (they buy the catalog its labels) and nothing is invented. Paths are absolute so the
    file works from wherever the engine's working directory happens to be."""
    missing = [book.name for book in books if not (book.zim_id and book.language)]
    if missing:
        raise ValueError(f"library form needs each book's id and language; missing for "
                         f"{', '.join(missing[:3])}{'…' if len(missing) > 3 else ''}")
    lines = ['<library version="1.0">']
    for book in books:
        attrs = [("id", book.zim_id), ("path", str(Path(book.path).resolve())), ("language", book.language)]
        if book.name:
            attrs.append(("name", book.name))
        if book.title:
            attrs.append(("title", book.title))
        if book.article_count is not None:
            attrs.append(("articleCount", str(book.article_count)))
        body = " ".join(f'{k}="{_xml(v)}"' for k, v in attrs if v)
        lines.append(f"  <book {body}/>")
    lines.append("</library>")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _xml(value: str) -> str:
    """The five XML entities, & first — the same order the feed decoder uses, so an escaped `&lt;` cannot
    be decoded twice into a `<` the book never held."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def attach_ids(books: list[Book], served_entries: list[dict]) -> int:
    """Fill id/language/title from the engine's own catalog, and report how many books were filled.

    Matching is by the `/content/<name>` link the catalog entry carries, which is the file stem — the
    catalog's own `<name>` field is a different, dateless name, so matching on it would miss every book."""
    by_name = {}
    for entry in served_entries:
        if entry.get("content_name"):
            by_name[entry["content_name"].lower()] = entry
    filled = 0
    for book in books:
        entry = by_name.get(book.name.lower())
        if not entry:
            continue
        book.zim_id = entry.get("id", "")
        book.language = entry.get("language", "")
        book.title = entry.get("title", "")
        if entry.get("article_count"):
            book.article_count = int(entry["article_count"])
        filled += 1
    return filled


def save_id_cache(books: list[Book], path: str | Path) -> Path:
    """Keep the ids a catalog read learned, so the library launch form works with the engine down.

    The cache exists because ids are only obtainable two ways — from a running engine, or by reading each
    file's header — and a deployment that relaunches a big set should not have to start the thing it is
    about to relaunch in order to know what to tell it. Only books whose id is known are written, and the
    file holds names, not paths: it is a lookup table, and a stale row is a missing row, never a wrong one."""
    payload = {book.name: {"id": book.zim_id, "language": book.language, "title": book.title,
                           "articles": book.article_count}
               for book in books if book.zim_id and book.language}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def load_id_cache(books: list[Book], path: str | Path) -> int:
    """Fill id/language from the cache, returning how many books it told us about.

    A missing or unreadable cache fills nothing and says so by returning zero: it is a speed-up for the
    library form, not a source of truth, and the launch path treats an incomplete set as "use paths or
    read the catalog", never as a licence to invent."""
    target = Path(path)
    if not target.is_file():
        return 0
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(raw, dict):
        return 0
    filled = 0
    for book in books:
        row = raw.get(book.name)
        if not isinstance(row, dict) or not row.get("id") or not row.get("language"):
            continue
        book.zim_id = str(row["id"])
        book.language = str(row["language"])
        book.title = str(row.get("title") or book.title)
        if row.get("articles") and not book.article_count:
            book.article_count = int(row["articles"])
        filled += 1
    return filled
