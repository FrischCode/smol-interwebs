"""SIW -- Smol InterWebs: the offline corpus a machine owns, served as a documented API.

A ZIM corpus (the Kiwix files: Wikipedia, the Stack Exchange network, API docs -- whatever the
operator downloaded) is served fulltext by `kiwix-serve`. SIW owns three things around it: the
configuration that says which directory and which books are the corpus, the lifecycle of the
`kiwix-serve` process that serves them, and an HTTP surface that answers JSON where the engine
answers HTML. Harnesses -- agentic or human -- read that surface and write their own adapter
against it; see the README and `web/CONTRACT.md`.

The modules are one concern each, and the dependency line runs one way:

    config   the one file the machine reads: corpus directory, picked books, endpoint
    corpus   the directory as facts: the books on disk, the picked subset, what is served
    engine   the process: probe, launch detached, heal, stop the one it started
    search   the routes over the engine: the request, the feed parse, the answer record
    api      the HTTP surface: the kernel's routes in, JSON out, the spec from the running server
    acquire  the download side: the queue, the spawn, the .part, the checksum, the resume
    console  the web console: /state, /action, the verbs, the page served from the same port
    __main__ the CLI: serve, status, doctor, stop

Nothing here imports a third-party package: the structural test holds that rule, and a
runtime dependency would need a decision written in `pyproject.toml` before it could exist.
"""

__version__ = "0.1.0"
