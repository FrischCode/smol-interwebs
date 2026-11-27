# SIW — Contributor Notes

Developer notes for **SIW — Smol InterWebs**. Read before editing.

The product overview is in `README.md`; the reasoning behind every decision is in the relevant source.

## What this is

An offline fulltext search server over the operator's ZIM corpus: a stdlib-only Python server that
owns a `kiwix-serve` engine (fetched, launched, recorded, stopped), a download queue, the books'
identity, and one loopback port that serves a documented JSON API, an MCP route, and a single-file
web console.

## Portability axiom

Everything must be copyable to another machine: a copy of the tree is the same product. The core
imports stdlib only — `kiwix-serve` is never imported, it is launched as a process (the pinned
binary, or the operator's `exe`) — and no runtime dependency may enter `pyproject.toml`
`dependencies` without the decision and its reason written in `pyproject.toml` first. Both Windows
and POSIX are targeted and must be supported; the suite must stand on both.

## The shipped tree stays environment-free

No drive letters but `C:` in examples, no account names, no network facts, no corpus stated as fact
(a count of *this* installation's books), no measurements stated as the developer's machine's, no
dev-process references ("the review", a session, a dated incident). The test
`test_no_module_names_the_box_it_was_developed_on` in `tests/test_structure.py` scans every shipped
text file and names the offender; development records live outside the tree. Test fixtures use
synthetic values or RFC-reserved address ranges. The pinned binary's facts (URL, sha256, size, PE
machine type) are facts about the artifact and are exempt — they are the security anchor.

## Self-explanatory

The tree carries every reason a reader needs — a comment, a docstring. Nothing bound to outside
source.

## Environment & commands

- Python `>=3.11`. The server is stdlib-only and runs under any interpreter; a venv holds only the
  dev tool (`pytest`). `siw-test.bat` (or `python -m pytest`) runs the suite.
- Run the server: `python -m siw serve` (the module is the entry point — `siw/__main__.py`), or
  `siw.bat` from this folder — the front door that also opens the console, extra arguments passed
  straight through. Verbs: `serve [--config PATH] [--port N] [--bind HOST]`, `status`,
  `doctor [--query WORD] [--library-ids]`, `stop`.
- Config resolution, first one found wins: `--config` > `$SIW_CONFIG` > `./siw.json` > the machine's
  config dir > shipped defaults. A gitignored `siw.json` beside the tree is where the machine states
  its own; `siw.example.json` is the committed shape. The field set is closed — an unknown key is a
  named refusal at load.
- The suite is hermetic: no real engine, no network, no machine config. The engine is a seam —
  probe, catalog, spawn and stop are injectable (`Console(probe=..., catalog=...)`,
  `engine.spawn_plan(..., platform=...)` names the branch under test) — and `tests/conftest.py`
  pins the machine dir at a path that never exists. A test that needed a real `kiwix-serve`, a real
  download or a real port could not ship with the repo.
- The face is one file, `web/index.html`: no build step, no runtime dependency, no CDN. Mock mode
  (`?mock=1` or `file://`) is the page's own test surface — the inline mock state and
  `web/state.mock.json` must stay field-for-field equal (a structural test enforces it).
- The wire contract is `web/CONTRACT.md`. The zero-hit sentences are contract, never prose: do not
  re-voice them in code, UI or docs.
- The pinned binary lives in `siw/tools.py`: URL, sha256, size and PE machine type, verified before
  anything launches. Changing any of the four requires re-measuring all four on the live download;
  the machine-type check is the 64-bit guarantee (a 32-bit `kiwix-serve` breaks on large ZIMs).
- The engine's pidfile record carries `image` — what the spawn will answer to
  (`engine.expected_image`: a script's interpreter, a binary's own name). A stop checks the record,
  never a constant; a record predating the field keeps the old constant on purpose.
- `examples/` are reference adapters for named harnesses (pi, SAH): the host names stay — they are
  the documented integration surface, not a box fact. The adapters know only the HTTP contract and a
  boot-time endpoint; they never spawn, discover or parse.
- Lint: ruff lives in the venv (dev-only; `dependencies` stays `[]`). `ruff check .` validates at the
  110-character ceiling in `pyproject.toml`; the layout is deliberate, so the repo is
  lint-enforced, not formatter-enforced. Suppression belongs in that config, never inline.

## Docstrings & comments

Standalone, local, universal: readable with only the file open and nothing else. No task/session/
date/incident prose ("as discussed", "the bug from yesterday"). A comment that references another
module names it. When a non-obvious decision needs to survive, the *reason* goes in a comment, not a
memory note — the idea must travel with the source.

## Editing

Keep this file token-dense: no prose, no filler, every line earns its tokens.

## Static

This file is not to be modified without operator consent.
