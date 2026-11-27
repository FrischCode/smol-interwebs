"""Offline Search — SAH reference adapter for SIW.

This file is the reference, not the product. SIW is a server; the harness writes this tool
against its documented API (`GET /spec` on the server; web/CONTRACT.md in the tree). Read it the way
you would read the contract it consumes: the SIW server owns the corpus, the pick, the
lifecycle and the ranking — this file only knows the HTTP contract and how to render it.
It contains no kiwix-serve logic: no spawning, no self-heal, no corpus discovery, no feed
parsing. Those are the server's acts.

Wire it in by copying it to the extensions folder and naming it in the run's extension list
(SAH_EXTENSION=extensions/sah-extension.py in your .env, or whatever you copied it as), and
set SIW_URL to the API's base (default http://127.0.0.1:7662 — SIW's default api_port).

The model picks the words and the narrowing; the endpoint travels as a construction fact,
exactly the split the shipped extensions use between the words and the address:

    SIW_URL     the API base, default http://127.0.0.1:7662
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from urllib.parse import urlencode
from typing import Annotated

from sah.contract import Param  # the Param annotation every SAH tool carries

SIW_URL = os.environ.get("SIW_URL", "http://127.0.0.1:7662")


def _render(record: dict, query: str) -> str:
    """The rendering is free-form; the record is the product. Keep it compact and honest:
    say what was searched, what came back, and what the zero means."""
    lines = []
    scope = record.get("scope") or {}
    configured = scope.get("configured")
    corpus = scope.get("corpus")
    served = scope.get("served")
    route = record.get("route", "?")
    healed = record.get("healed_in_seconds")
    header = f"searched {configured if configured is not None else '?'} books ({corpus or '?'}) — {route}"
    if served is not None:
        header += f", engine serves {served}"
    if healed is not None:
        header += f", healed in {healed} s"
    lines.append(header)
    if record.get("heal_note"):
        lines.append(f"  {record['heal_note']}")
    if record.get("zero_hit"):
        lines.append("")
        lines.append(record["zero_hit"])
    results = record.get("results") or []
    endpoint = record.get("endpoint") or ""
    for i, r in enumerate(results):
        url = r.get("url")
        url_part = f" — {endpoint}{url}" if url else ""
        lines.append("")
        lines.append(f"{i + 1}. {r.get('title') or '(no title)'} — {r.get('book') or '?'}{url_part}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
    if results:
        lines.append("")
        count, total = record.get("count", 0), record.get("total", 0)
        lines.append(f"{count} shown, {total} in index (total for {query!r})")
    return "\n".join(lines)


class SiwSearch:
    """One tool bound to the SIW the operator points it at: the API's contract, a GET, a
    JSON parse, a rendering. No engine logic here — the server owns the lifecycle."""

    def __init__(self, url: str = SIW_URL, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.offline_search = self._make_tool()
        self.tools = (self.offline_search,)
        self.findings = (self._probe(),)

    def _probe(self) -> str:
        """The boot fact: is the API answering when the run starts? A refused connection is
        a fact in its own right — a run that does not know its search is down will burn a
        turn discovering it. It costs one localhost round trip when up."""
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=3) as res:
                record = json.loads(res.read().decode("utf-8"))
            if record.get("ok"):
                return f"offline_search: SIW at {self.url} is answering; offline knowledge is reachable"
            return f"offline_search: SIW at {self.url} answers, but reports not ok (engine down?)"
        except (urllib.error.URLError, OSError, ValueError):
            return f"offline_search: SIW at {self.url} is not answering; start it with `siw serve`"

    def _make_tool(self):
        url, timeout = self.url, self.timeout

        def offline_search(
            query: Annotated[str, Param("the search query — words or a phrase; the index ANDs all terms")],
            limit: Annotated[int, Param("maximum results to return (default 5, ceiling 25)")] = 5,
            sources: Annotated[
                str,
                Param("narrowing by book name: comma/space-separated substrings "
                      "(exact names at `GET /catalog`); only matching books are searched"),
            ] = "",
            corpus: Annotated[
                str,
                Param("narrowing by family substring, e.g. 'wikipedia'; empty = all"),
            ] = "",
        ) -> str:
            words = query.strip()
            if not words:
                return "offline_search: nothing to search for (the query is empty)"
            n = max(1, min(int(limit or 5), 25))

            params = {"q": words, "limit": n}
            if sources.strip():
                params["sources"] = sources.strip()
            if corpus.strip():
                params["corpus"] = corpus.strip()
            full = f"{url}/search?{urlencode(params)}"

            try:
                with urllib.request.urlopen(full, timeout=timeout) as res:
                    record = json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # An error status carries the one error shape in its body: render the code and
                # the remedy sentence, never a traceback.
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                except (ValueError, OSError):
                    body = {}
                err = body.get("error") or {}
                code = err.get("code", "crash")
                message = err.get("message", "the server did not answer")
                return f"offline_search: {code} — {message}"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                return (f"offline_search: SIW at {url} is not answering ({exc}); start it with "
                        "`siw serve` — the engine is the server's act, not the query's")

            return _render(record, words)

        # `self`, beside the shipped extensions' own address guard: the endpoint is fixed at
        # boot by what the operator set (the env var, beside SAH_HOST), and nothing the model
        # can direct has grown. There is no address to declare.
        offline_search.access, offline_search.address = "self", None
        return offline_search


def from_env() -> SiwSearch:
    return SiwSearch(url=os.environ.get("SIW_URL", SIW_URL))


# The one name the door reads. An instance, not a class.
EXTENSION = from_env()
