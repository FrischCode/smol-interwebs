"""siw -- the command. The console server is the front door; the rest are the operator's verbs.

    siw serve                the console + API on one port (config: --config PATH, --port N, --bind A)
    siw status               the whole snapshot, once, as JSON (no server started)
    siw doctor               the checks: config, corpus, engine, port, smoke search
                             (--query WORD runs that smoke query; --library-ids caches the
                             book ids a running engine's catalog knows, for the library form)
    siw stop                 stop the engine this config started (its pidfile, its port)

`serve` runs until Ctrl-C. `status` is the answer an agent gets without a live server: the same
shape the console polls, read once from disk and the process table.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from siw import config as config_module
from siw import doctor, engine

HELP = __doc__


def _flag(rest: list[str], name: str) -> str | None:
    """One --flag value (or --flag=value) out of the remainder. Deliberately hand-rolled: the verb
    set is small and argparse's error style would out-shout the tool's."""
    for i, arg in enumerate(rest):
        if arg == name and i + 1 < len(rest):
            return rest[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _has(rest: list[str], name: str) -> bool:
    """A valueless --flag out of the remainder."""
    return any(arg == name for arg in rest)


def _config(rest: list[str]) -> object:
    """The config for this run, plus the flags that override it. `--config PATH` replaces the
    search chain; `--port` and `--bind` are run-only overrides (the file is not written by them)."""
    explicit = _flag(rest, "--config")
    if explicit:
        values, _layer = config_module.load(Path(explicit))
        cfg = config_module.Config(values, path=Path(explicit))
    else:
        cfg, _where = config_module.resolve()
    port = _flag(rest, "--port")
    if port:
        try:
            cfg.set("api_port", int(port), source="flag")
        except ValueError:
            raise SystemExit(f"siw: --port wants an integer, got {port!r}") from None
        except config_module.ConfigError as exc:
            raise SystemExit(f"siw: {exc}") from None
    bind = _flag(rest, "--bind")
    if bind:
        cfg.set("bind", bind, source="flag")
    return cfg




def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verb = argv[0] if argv else "serve"
    rest = argv[1:]
    if verb in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if verb == "serve":
        from siw.console import serve
        return serve(_config(rest))
    if verb == "status":
        from siw.console import Console
        console = Console(_config(rest))
        print(json.dumps(console.state(), indent=1))
        console.acquirer.stop()
        return 0
    if verb == "doctor":
        cfg = _config(rest)
        code, lines = doctor.report(cfg, smoke_query=_flag(rest, "--query") or "",
                                    library_ids=_has(rest, "--library-ids"))
        for line in lines:
            print(line)
        return code
    if verb == "stop":
        cfg = _config(rest)
        ok, note = engine.stop(cfg)
        print(note)
        return 0 if ok else 1
    print(f"siw: unknown verb {verb!r} (serve, status, doctor, stop)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
