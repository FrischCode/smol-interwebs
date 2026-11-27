# CONTRACT.md — the console's wire shape

What `web/index.html` speaks to the SIW server, and what the Python backend must serve.
Two routes, one origin as the page itself (`http://127.0.0.1:7662`):

| route | method | what |
|---|---|---|
| `/state` | `GET` | the whole console snapshot, one JSON object. No body, no parameters. |
| `/action` | `POST` | `{"verb": str, "args": object}` → one JSON object. |

Rules that hold for both:

- **Same origin.** The page is served by the SIW server; do not put these routes on another port.
  No response carries `Access-Control-Allow-Origin` — nothing on this port is offered to another
  origin. `/action` additionally rejects cross-origin writes: a POST whose `Sec-Fetch-Site` says
  `cross-site`/`cross-origin`, or whose `Origin` names any host but this one, is answered
  `403 cross_origin`. The console page also sends `X-Requested-With: siw`, which a plain form post
  cannot carry. Non-browser clients send neither header and are unconstrained.
- **Credentials.** When the server is bound beyond loopback, the routes require the configured
  credentials (`Authorization: Basic`); loopback needs none. A non-loopback bind with no password
  is *refused at boot* — whichever layer named the bind (config file, `SIW_BIND`, `--bind`) — and
  `set_settings` refuses to save the combination (`400 credentials_required`). Nothing is served
  unauthenticated beyond loopback. The password lives in the config file in plain text; the
  transport has no TLS.
- **Answer with the engine down.** `/state` must never require the engine. It answers fully — including
  the failure that needs recovering — when kiwix-serve is dead. It must also never touch the internet
  (the `.meta4` read belongs to `preview`, which is allowed to be slow and to fail).
- **A plain start is a working server.** `siw serve` probes the engine's port on boot: dark ⇒ bring it
  up the way `engine_start` would (fetching the pinned binary first when the config names none, so the
  first machine's fetch is the first thing the page shows). A port that already answers — foreign or
  own — is left alone. The fetch itself is the one network act a boot performs, and it is pinned:
  `siw/tools.py` names the URL, the sha256, and the size.
- **Be quick.** The page polls this every 2–5 s. Bound the engine probe to ~300 ms and cache the
  directory scan (~5 s) rather than walking the corpus volume per poll; `generated_at` says which one is stale.
- **Errors.** `{"ok": false, "error": {"code": str, "message": str}}` with the HTTP status that names
  the kind (`400` bad args, `404` unknown verb/row, `409` well-formed but refused now, `500` bug,
  `503` needs the engine). One error path, as `siw/api.py` already does. Success: `{"ok": true, "result": object}`.
- Unknown `verb` → `404` naming the known verbs; unknown key inside `args` → `400`. An accepted-and-
  ignored argument is the failure this contract promises against.

---

## 1. `GET /state`

```json
{ "ok": true, "product": "siw", "name": "Smol InterWebs", "version": "0.1.0",
  "generated_at": "ISO-8601", "poll_after_ms": 2000,
  "server": {...}, "engine": {...}, "pending_apply": [...], "corpus": {...},
  "catalog_link": "https://browse.library.kiwix.org",
  "rows": [...], "tasks": [...], "notices": [...] }
```

| field | type | meaning |
|---|---|---|
| `ok` | bool | always `true` on 200; present so a client can branch on one key |
| `product`/`name`/`version` | str | this server, printed in the header |
| `generated_at` | str ISO-8601 UTC | when the snapshot was taken |
| `poll_after_ms` | int | **the server's advice**; the page clamps to 500…30000 and falls back to 2000 (work active) / 5000 (idle) when absent |
| `server` | obj | the SIW server itself (bind, credentials) |
| `engine` | obj | kiwix-serve |
| `pending_apply` | array | saved settings that no running process has taken. `[]` when there is nothing pending |
| `corpus` | obj | the ZIM directory: location, room, what the last scan found |
| `catalog_link` | str\|null | the "get more ZIMs here" target (client-side default: `https://browse.library.kiwix.org`) |
| `rows` | array | one entry per book/**build** — catalog, queue and inventory in one list |
| `tasks` | array | long operations with progress (the corpus move). `[]` normally |
| `notices` | array | one-line messages that need a decision, above the list |

### `server`
| field | type | meaning |
|---|---|---|
| `bind` | str | address the API listens on |
| `port` | int | port. 7662 is the console's default |
| `url` | str | the address to click (`http://127.0.0.1:7662`) |
| `loopback` | bool | true only when every bound address is loopback. Drives the credential rule |
| `bound_on` | array\<str\> | what the socket says it answers on (`["0.0.0.0"]` ⇒ loud warning without credentials) |
| `credentials` | obj | `{mode: "none"\|"basic", configured: bool, username: str\|null}` — **never the password**, not even redacted |
| `warning` | str\|null | a refusal the server is currently in (`bound 0.0.0.0 without credentials`). Rendered loud and red; the page mirrors its own form's violation here while typing |
| `uptime_seconds` | float\|null | since this process bound |
| `config_path` | str\|null | the file these values came from |
| `platform` | str | printed in Runtime |

### `engine`
| field | type | meaning |
|---|---|---|
| `state` | str | `"up" \| "down" \| "starting" \| "stopping"` — the lifecycle truth, `up` iff a live probe answered |
| `up` | bool | `state === "up"`, kept both because a client asserts on a bool |
| `host`,`port`,`url` | str,int,str | where it serves. 8080 in the mock |
| `exe`,`version` | str\|null | the binary and `kiwix-serve --version` |
| `form` | str\|null | how the set reached it: `"paths" \| "library"` (the kernel's `launch`) |
| `bound_on` | array\<str\> | where the engine's port answers |
| `pid` | int\|null | SIW's own child, when it started one |
| `started` | str\|null | when SIW launched it (the pidfile record's start, ISO); `null` when it did not launch one |
| `started_by_siw` | bool | false ⇒ stop/restart are offered with a warning: the console would be killing a server it did not launch |
| `served_books` | int\|null | the engine's own catalog count — `null` when down (never 0: 0 is a *drift* finding, `null` is *nobody looked*) |
| `configured_books` | int | how many rows the pick says should be served |
| `drift` | obj\|null | `corpus.diff()` output (`configured`,`served`,`same`,`configured_only`,`served_only`); `null` when down |
| `problem` | str\|null | why it is not up, in one sentence with the remedy in it. `null` for a plain down: a dead port needs no explanation. Non-null when there is something to do about it: a binary being fetched (with live percent), a failed download, a configured binary that is not on disk, a spawn that just failed |
| `log_path` | str\|null | engine log, printed so the human can read it |
| `uptime_seconds` | float\|null | engine's, not the console's |
| `last_checked` | str ISO | when the probe last ran |
| `bin` | obj | the binary's truth (below) |

`engine.bin` — `{state, version, path, exists, progress, error}`. `state`: `"configured"` (the config
named an `exe`; `exists` says whether that file is on disk), `"fetched"` (the pinned binary SIW
installed itself — the config's source map is the record, and `path` is in the machine's tools dir),
or the fetch's own words — `"missing"` (the pinned binary is not on this machine yet),
`"fetching"` / `"verifying"` / `"extracting"` (in flight, `progress = {received, total}`),
`"ok"` (verified by sha256 and extracted; `path` is the binary to spawn), `"failed"` (`error` is
the reason; `start` retries). The default config names no `exe`: the engine's binary is *fetched,
verified, and installed* by SIW into the machine data dir — the console shows that fetch as part of
the engine's state, and `engine_start` is what starts it. A configured `exe` is the user's: SIW never
fetches over one, and a missing configured file is a `binary_missing` notice, not a download. The
pin (URL, sha256, size) is a repository constant — `siw/tools.py`.

### `pending_apply[]`
`{field: str, label: str, from: any, to: any, requires: "engine_restart"|"server_restart"}`.
One entry per changed field. `apply` makes them real (restarting what it must); `discard` throws the
saved change away. The badge must never be the only hint: each entry prints `field: from → to`.

### `corpus`
| field | type | meaning |
|---|---|---|
| `directory` | str\|null | the ZIM folder — **the intake**: a file copied in by any means is a row after the next scan |
| `exists` | bool | false ⇒ every on-disk row is wrong and the panel says so loudly |
| `bytes_free`,`bytes_total` | int\|null | volume room. Compared against the pending selection |
| `books` | int | complete `.zim` files found (excludes `*.zim.part`) |
| `bytes` | int | their total size |
| `parts[]` | `{name,bytes}` | in-flight transfers as the house convention sees them |
| `scanned_at` | str\|null | the scan's age is printed; `scan` forces a fresh one |
| `scan_errors[]` | array\<str\> | unreadable entries — never swallowed |

### `rows[]` — the row state machine
`absent → queued → downloading → downloaded → served`, with `failed` reachable from
`queued`/`downloading`. **If it is present, it is served**: the served set is the corpus directory
itself — there is no per-book opt-in (no `serve`/`unserve`), and `downloaded → served` is only the
engine's confirmation, never a human action. Identity is resolved the same way: automatically, at
verify/scan time (filename stem first, then the ZIM's own embedded metadata) — `ident` is *set* by the
server, never requested. A row is a **book build**, not a file: the catalog row and the file it
produced are the same row (a file already in the folder that matches a suggested build flips that row's
state instead of adding a duplicate). How a book arrived is not a row property — a file in the folder is
a book like any other.

| field | type | meaning |
|---|---|---|
| `id` | str | unique, **stable across polls**. Catalog rows `<book>/<build>` (`wikipedia/mini`) or `<book>` (`unix`); non-catalog rows the file stem. Used as the `data-id` of every action |
| `book` | str | the list's subject, one token if you can (`devdocs python`). Mono in the UI |
| `title` | str | human name, used for tooltips and previews |
| `build` | str\|null | `mini`/`nopic`/`maxi`, or `null` for single-build books |
| `builds` | array\<str\>\|null | the variants this book offers — **the row is a sub-row iff this is non-null**; `null`/`[]` for single-build. Order is display order. In the offered set every catalog row has `builds: null` — one build per book; the axis stays in the wire format for multi-build books |
| `media` | str | `"text" \| "text+images" \| "unknown"` — from the engine's catalog (`mediaCount`) while the book is served, else the offer's stated value, else `unknown` |
| `size_bytes` | int | the size that matters: on disk if there is a file, else the expected download size. Everything the footer sums is this field |
| `state` | str | see the machine above; one value, exclusive |
| `defaulted` | bool | in the default download set. The footer's sum and `download_all` cover the `absent` rows where this is `true` - the button's number and the button's act are the same number. In the minimal set every offered row is `true`: the console list **is** the default download set, and the footer sums exactly the table (the 107 G stack overflow book was cut from the set 2026-11-26 so the total can never silently skip a visible row) |
| `note` | str\|null | one human sentence: why a build is worth its bytes, "waiting for superuser", … |
| `ident` | obj\|null | resolved identity — automatic at verify/scan time, never an action; `null` until the file is on disk and the scan has run |
| `file` | obj\|null | the on-disk file; `null` for absent/queued/downloading/failed |
| `served` | obj\|null | serving state; `null` unless the file is on disk (`downloaded` or `served`) |
| `progress` | obj\|null | `queued` and `downloading` only |
| `error` | obj\|null | `failed` only |
| `source_url` | str\|null | where it comes from / came from |
| `actions` | array\<str\> | the verbs the backend allows for this row now. **Authoritative when present**; the page falls back to deriving them from `state` |

`ident` = `{rung: "filename"|"metadata", name: str, title: str|null, date: str|null, confidence: "high"|"low"}`.
`rung` is the identity ladder's answer and is **printed** ("identified · filename") — provenance is the
whole point of the state. Two rungs only: filename stem, then the ZIM's own embedded metadata.

`file` = `{name, path, bytes, mtime, sha256: str|null, verified: str|null}`. `verified` names how a
finished transfer checked its bytes — `"sha256"` (compared against the checksum) or `"header"` (the
ZIM magic and length read back from disk); `null` = not verified. `sha256` is `null` until it is known.

`served` = `{configured: bool, confirmed: bool, seen_at: str|null, url: str|null, page_url: str|null}`.
`configured` is `true` for **every on-disk file** — the served set is the corpus directory itself,
there is no per-book opt-in. `confirmed` = the **running engine reported this book in its catalog
during this poll**. `page_url` is the served page URL passed through verbatim from the engine —
it is the agent's "show me" deliverable, so do not rewrite or shorten it.

`progress` = `{bytes, total_bytes, percent, rate_bytes_s: int|null, eta_seconds: int|null,
queue_position: int, url: str|null}`. `rate`/`eta` may be `null` for a queued row or a fresh child.
`queue_position` is 0 for the active transfer, 1..n for those waiting.

`error` = `{code: str, message: str, retryable: bool, attempts: int, failed_at: str, partial_removed: bool}`.
`message` is shown as-is (e.g. "checksum mismatch — the file hashes to `11c4…`, the catalog expects
`7d90…`"). `partial_removed` tells the human whether the bytes are gone or the retry resumes.

### `tasks[]`
`{id, kind: "move_corpus", label: str, state: "queued"|"running"|"done"|"failed"|"cancelled",
bytes: int, done_bytes: int, rate_bytes_s: int|null, eta_seconds: int|null, from: str, to: str,
error: str|null}`. Only a move is long enough to deserve one. `done`/`failed`/`cancelled` entries may
be kept for a while so the result is visible, then dropped.

### `notices[]`
`{id: str, level: "info"|"warn"|"bad"|"ok", text: str, since: str, action: {verb: str, label: str}|null}`.
`id` is stable while the condition holds (the page keys DOM on it, so a live action label does not
re-render away). The ✕ calls `dismiss`. Condition ids that occur today: `download-done:<row>` (ok),
`engine-down`, `binary_failed` (the pinned download failed; the action is `engine_start`, labelled
Retry download), `binary_missing` (a configured `exe` that is not on disk), `corpus-missing`,
`over-capacity`, and `foreign-corpus` (the engine answers and serves more books than the corpus
directory accounts for — it was started elsewhere, against a corpus this config does not name).
A fetch in flight earns no notice: the engine line already shows the percent.

---

## 2. `POST /action`

Request `{"verb": str, "args": {}}`. Response `{"ok": true, "result": {...}}`; `result` may be `{}`.
Every verb returns the freshest truth it has; the page re-polls `/state` immediately after any action
anyway, so **no verb needs to return the full state**. `args` keys not listed are rejected.

| verb | args | result | notes / errors |
|---|---|---|---|
| `engine_start` | — | `{engine}` | the whole act: ensure the binary (fetching the pinned one when the config names none — the answer is 200 with the fetch in flight, and the landed binary starts the engine itself), then spawn detached and wait for the port (~10 s). A port that already answers returns the snapshot as-is (a foreign engine is never touched). `409 binary_fetching` (a download is already in flight), `409 corpus_empty`, `502 spawn_failed` (message quotes the log path) |
| `engine_stop` | — | `{engine}` | kill only a child SIW started; `409 foreign_process` when the port belongs to someone else |
| `engine_restart` | — | `{engine}` | stop+start. Down ⇒ behaves as start |
| `config_apply` | — | `{pending_apply, engine}` | write pending fields to the config, restart what they need. `[]` pending ⇒ no-op, `ok:true` |
| `config_discard` | — | `{pending_apply}` | throw away the un-applied change; the config file returns to the running values |
| `download` | `id` | `{row}` | start one row. **Idempotent**: already queued/downloading ⇒ no-op returning the row (double-click cannot start two children). `409 no_space` (`{needed, free}`) when it cannot fit |
| `download_all` | — | `{started: [id]}` | the footer's single button ("Start all downloads"): every `absent` row **of the default set** (`defaulted: true`), in list order, one transfer at a time - the same rows the footer sums, so the confirmed number is the number that runs. Rendered only while at least one such row exists |
| `cancel` | `id` | `{row}` | kill the child, keep the `.part` for a resume. Row returns to `absent` (`note` says a part is on disk) |
| `retry` | `id` | `{row}` | re-enter a `failed` row at `queued`, attempt count incremented |
| `remove` | `id` | `{rows}` | delete the file from disk. Rows for files not in the suggested set leave the list; suggested-set rows return to `absent`. Confirm-before-delete is the page's job; the backend just does it |
| `scan` | — | `{corpus, added: [id]}` | force the directory scan. The only way a copied-in file appears |
| `preview` | `url: str` | `{preview}` | the paste field's gate. `{kind:"zim"\|"catalog_entry", url, name, title, size_bytes, sha256\|null, checksum_source, book, build, media, warning}`. Reads the `.meta4` sidecar; nothing is written, nothing is downloaded. `400 unrecognized_url`, `503 preview_unreachable` (offline / host refused) |
| `download_url` | `url: str, sha256: str\|null` | `{row}` | the **confirmed** paste. Must be preceded by a successful `preview` for the same URL (`409 preview_required`) - never a silent fetch. **A user-supplied `sha256` is the checksum of record**: the transfer verifies against it and no sidecar is fetched. Without one the queue still verifies before promotion — the `.meta4`/`.sha256` sidecar when the host offers it, the ZIM header otherwise. New row |
| `set_settings` | any of `bind, port, username, password` | `{server, pending_apply}` | omitted keys unchanged; `password:""` clears it. `400 credentials_required` when the resulting bind is non-loopback with no password; `400 bad_port`; `409 bind_taken`. Every accepted change lands in `pending_apply` |
| `set_corpus_dir` | `path: str, move: bool` | `{corpus}` or `{task}` | with a populated corpus `move:false` is refused (`409 move_required`) — a silent re-point hides 300 GB. `move:true` returns `{task}` and the page watches `tasks` |
| `task_cancel` | `id` | `{tasks}` | cancel a running move |
| `open_folder` | — | `{opened: str}` | open the ZIM folder in the OS file manager, creating the folder first when it does not exist yet (a first-run machine's default corpus dir is exactly that case). Loopback-only; opens nothing else |
| `dismiss` | `id: str` | `{notices}` | stop showing a notice until its condition reoccurs |

---

## 3. Polling contract

1. The page reads `GET /state` on a timer and re-renders the whole snapshot; it keeps no state of its
   own beyond in-progress UI (focus, typed-but-uncommitted fields, armed confirmations).
2. Interval: **`poll_after_ms` when present**, else 2000 ms while any row is `queued`/`downloading` or
   any task is `running`/`queued`, else 5000 ms. The page clamps to 500…30000.
   Emitting `poll_after_ms` is how the backend says "come back in 200 ms, a transfer just started" or
   "nothing changes for a minute".
3. A successful `POST /action` triggers an immediate poll (the page does not wait for the timer).
4. While the tab is hidden the timer keeps running but the poll is skipped, and one poll fires on
   `visibilitychange`. A page in a background tab must not be the reason a directory scan runs all day.
5. On `/state` failure: keep the last render, show `state unavailable`, and back off to a 5000 ms
   floor (doubling to 30 s while it keeps failing). A dead snapshot beats a blank page — but the
   actions are disabled the moment the state is older than 30 s, because the truth behind them is gone.
6. The renderer must be idempotent: same `/state` bytes ⇒ the same DOM. The page skips table rebuilds
   when the rows' signature is unchanged, and defers them while any editable field has focus, so a
   poll never eats a half-typed value.

---

## 4. What the UI derives (do not send it twice)

- **Footer sum** — Σ `size_bytes` where `state === "absent"` **and `defaulted`** ⇒ `+14.1 GB to
  download`, with the count: the default set's cost, which is exactly what `download_all` starts
  (the two must agree - the number is the confirmation). Compared against `corpus.bytes_free`:
  over ⇒ red, and `download_all` is disabled (mirroring the backend's `409 no_space`).
- **Retry line** — the failed rows' `size_bytes` shown separately ("1 failed · 1.5 GB to retry"), so
  the headline number stays honest.
- **Badge colour** — `served` + `confirmed` green · `served` without `confirmed` amber, with the reason
  ("engine down — last seen 10m ago" when `engine.up` is false, "in the pick, not in the engine's
  catalog" when it is true: that is drift, and it is actionable) · `queued`/`downloading` amber (in
  progress) · `downloaded` blue (hash-verified, on disk) · `failed` red · `absent` dim.
- **The book name is the open action.** A `served` row with `served.page_url` renders its book name as
  a link to that page — there is no separate Open button. Rows not served keep a plain name.
- **The action column holds exactly one button** — `Download` (absent) · `Cancel` (queued/
  downloading) · `Remove` (everything on disk, and `failed` too). The `retry` verb is rendered in the
  download column of a `failed` row, where the download story lives.
- **Sub-rows** — rows sharing a `book` render as one book with sub-rows (the first
  row carries the name, the rest an indent). This is derived from **adjacency**, so the backend must
  keep a book's build rows adjacent and in `builds` order (`mini, nopic, maxi`). In the
  minimal set this never fires — one build per book.
- Unknown `state` values render as an amber badge with the raw value printed. Never drop a row the
  page does not recognise: an invisible 118.7 GB row is worse than an ugly one.

---

## 5. Mock mode

- Enabled by `?mock=1` or by opening the file directly (`file://`), where `fetch` is blocked anyway.
  The mock lives in the inline `MOCK_STATE` constant; `state.mock.json` is the same payload, written
  out for the backend to diff against.
- The mock simulates locally: transfer progress, queue promotion, hash-verify with identity resolved
  automatically on completion, engine start/stop (on-disk rows flip to `confirmed` when it comes up),
  previews, the corpus move task, `set_settings` landing in `pending_apply`. Anything it invents at
  runtime lives under a `_sim` key on its own state object — clients must ignore keys starting with `_`.
- `state.mock.json` deliberately shows every state at once, with the engine **down**:
  5 absent rows (13.4 GB pending), 1 served-but-unconfirmed (last seen stamped), 1 queued
  (behind the active transfer), 1 downloading at 43 %, 2 downloaded (both identified on scan,
  the served set waiting for the engine's confirmation), and 1 failed on a checksum. One
  `pending_apply` entry (the engine's launch form) shows the pending-apply banner; `bytes_free`
  is 96 GB, so the over-capacity path is reachable via the paste field (a big catalog URL),
  not from the default screen. The mock's source URLs use the `lb.download.kiwix.org` host,
  exactly as the offered set does.
