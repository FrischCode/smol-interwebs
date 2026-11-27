# Reference adapters — the thing a harness mimics

SIW ships **no per-language extension**. It ships a server with a documented contract
(the wire contract [`web/CONTRACT.md`](../web/CONTRACT.md) for the human, the OpenAPI
`docs/api.json` -- served live at `/spec` -- for the agent — the running server serves the same document at `/spec`). The two files in this
folder are the reference for what a harness writes on top of that contract:

| file | for | shape |
|---|---|---|
| [`pi-extension.ts`](pi-extension.ts) | the pi harness | one tool, one GET, one text render |
| [`sah-extension.py`](sah-extension.py) | the SAH harness | one tool, one GET, one text render |

Thin on purpose. Both contain no kiwix-serve knowledge: no spawning, no self-heal, no
corpus discovery, no feed parsing. The SIW server owns the lifecycle (`siw serve`,
`siw stop`, `siw doctor`), the pick, the ranking and the record. The adapter only:

1. reads the query the model picks,
2. GETs `/search` (or `/health` for a boot fact),
3. renders the record as text for the model.

## The loop

```sh
siw doctor                            # 0 healthy, or the first thing that is wrong
siw serve                               # console + API on one port; the page starts the engine
siw status                              # the whole snapshot once, as JSON (no server started)
```

Then each harness gets one file:

**pi** — copy `pi-extension.ts` into your pi extensions folder, set `SIW_URL` (default
`http://127.0.0.1:7662`), restart pi. That is the whole wiring. The extension registers
`offline_search` with its host bound at import (the env var) — the same split the harness
keeps between the words and the address.

**SAH** — copy `sah-extension.py` into the extensions folder, set
`SAH_EXTENSION=extensions/sah-extension.py` (or the file you copied it as), set `SIW_URL`,
and the run gets one tool: `offline_search`. Same idea, Python side.

## What the adapters deliberately do NOT do

* they never spawn or restart the engine — that is the server's act;
* they never discover the corpus or enumerate the books — that is the config + `/catalog`'s act;
* they never parse the feed — the server returns the record;
* they never name the engine's address — that is the operator's act at the config (the
  env var), exactly the confine rule the shipped extensions already use;
* they never ask the server for anything it does not serve — unknown parameters 400, and
  the adapter renders the error shape (`{"error": {"code", "message"}}`) as the answer.

The point of the examples is not that they are the right answers — they are readable
one-of-each, the shape a good adapter takes. A harness that wants more (a status route,
an indexer, an integration with its own UI) writes its own file against the same contract.

**This is a server that delivers LLM-ingestible results. Here is how to pull those from
it. Now tell your agent to write its own extension so it can use it.**
