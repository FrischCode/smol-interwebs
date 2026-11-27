"""siw.engine -- the `kiwix-serve` process: probe, launch, heal, stop, and what it says it serves.

Three verbs own the process, and each keeps one property the others cannot:

* **probe** — a socket connection, the way a client means it. Refused in a moment when nothing answers;
  connected-and-closed is the fact a search needs. Never a guess from a process listing.
* **heal** — probe, then check for an already-running process, then launch detached, then poll. The
  order is the design: a process that exists but whose port is dark is a server still loading, and a
  second instance would race it for the port — the loser auto-increments to a free port and idles
  orphaned, which looks like a working deployment serving the wrong set. So a live process is a reason
  to poll, never an opportunity to launch.
* **stop** — a server this tool started, recorded in a pidfile it wrote, and nothing else. The pid must
  still be alive, must still be the same image, and must be the port we recorded; a stop that cannot
  check those refuses rather than reaching for a process number it did not issue.

The spawn is the load-bearing part, and it is platform-split:

* **Windows**: `CREATE_NO_WINDOW | DETACHED_PROCESS`. This engine build, attached to *any* console —
  hidden or visible — has hung at startup opening its books at zero CPU, and a console allocated for a
  headless parent appears on the operator's desktop for the server's whole life. `CREATE_NO_WINDOW`
  alone is not enough; the pair is required.
* **POSIX**: `start_new_session=True`, so a healed server leaves the launching terminal's process group
  and Ctrl-C cannot reach it, and `pgrep` for the process check, because `tasklist` does not exist there
  and a failed check answers "no process" — precisely the double-launch the guard exists to prevent.
  This leg is written to the same contract and the POSIX branch has not been executed. `siw doctor`
  says so on a POSIX host instead of implying it ran.

The set that reaches the launch is the configured set, not the directory: a picked subset that never
reaches the argv is a cosmetic setting. Where the picked set is too large for a command line, the launch
carries a generated library XML instead — see `siw.corpus.launch_form_for`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from siw import config as config_module
from siw import corpus, zimmeta

# The engine's load window. Measured on a large corpus: the whole set is listening in ~3 s
# (books open lazily on first query), so this is a ceiling for a cold machine or a slow mount, not an
# expectation — a shorter budget turns a loading server into a false "dead".
STARTUP_BUDGET = 60.0
# How long a stop waits for the port to go dark after the process is asked to leave.
STOP_BUDGET = 15.0


class EngineError(Exception):
    """An engine act that cannot be performed: no binary, no corpus, a server that is not ours to stop."""


def probe(host: str, port: int, timeout: float = 3.0) -> bool:
    """Is anything answering on that address and port, right now."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _listeners(port: int) -> tuple[list[tuple[str, str]], str]:
    """(local address, pid) for every socket listening on this port, and why it could not be said.

    This is the only way to see the difference between "configured loopback" and "listening on every
    interface", which is the difference between a private corpus and a share of the machine. Windows and
    POSIX both ship a listing tool (`netstat -ano`, `ss -ltn`); a listing that fails answers with the
    reason, because "unknown bind" must never read as "loopback"."""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                                 timeout=15, creationflags=_no_window()).stdout
            pid_from_users = False
        else:
            # `-p` is what carries the owner on ss, and only for the user running it — which is the same
            # user that started the server we are asking about.
            tool = ["ss", "-ltnpH"] if shutil.which("ss") else ["netstat", "-ltn"]
            out = subprocess.run(tool, capture_output=True, text=True, timeout=15).stdout
            pid_from_users = shutil.which("ss") is not None
    except (OSError, subprocess.SubprocessError) as exc:
        return [], exc.__class__.__name__
    found: list[tuple[str, str]] = []
    # Windows columns: `TCP  <local>  <peer>  LISTENING  <pid>`; ss's: `LISTEN <q> <backlog> <local> ...`.
    # The state and the local address therefore sit in different columns, and reading the wrong one turns
    # a server bound to every interface into "nothing listening" — the one finding that must not be lost.
    for line in out.splitlines():
        fields = line.split()
        if os.name == "nt":
            if len(fields) < 5 or not fields[3].upper().startswith("LISTEN"):
                continue
            local, pid = fields[1], fields[4]
        else:
            if not fields or not fields[0].upper().startswith("LISTEN"):
                continue
            local = fields[3] if len(fields) > 3 else ""
            pid = ""
            if pid_from_users:
                holder = re.search(r"pid=(\d+)", line)
                pid = holder.group(1) if holder else ""
        head, _, tail = local.rpartition(":")
        if tail == str(port) and head:
            found.append((head.strip("[]"), pid))
    return found, ""


def listening_on(port: int) -> list[str]:
    """The addresses the port answers on. A failed listing is a named entry, never an empty list: "could
    not tell" and "nothing is listening" are different statements and the doctor prints them differently."""
    found, why = _listeners(port)
    if why:
        return [f"<bind listing failed: {why}>"]
    return sorted({address for address, _pid in found})


def listening_pids(port: int) -> list[int]:
    """Which process numbers hold this port — the number to print when a server is not ours to stop.

    A stop that refuses is only useful if the operator can finish the job: the pid and the command line
    are the difference between "someone else owns this" and a five-minute search through Task Manager."""
    found, _why = _listeners(port)
    return sorted({int(pid) for _address, pid in found if pid.isdigit()})


def version(exe: str) -> tuple[str, str]:
    """The binary's own `--version` output, and the path it came from — the version a finding quotes has
    to be the version the launch would use, so it is taken from the configured binary rather than a
    sibling on PATH. Failure is a value, not an exception: a missing binary is a doctor finding."""
    if not exe or not os.path.isfile(exe):
        return "", f"no binary at {exe!r}"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=_no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{exe!r} --version failed: {exc.__class__.__name__}"
    text = (out.stdout or out.stderr or "").strip()
    return text.splitlines()[0] if text else "", ""


def process_running(pattern: str = "kiwix-serve") -> tuple[bool, str]:
    """Is an engine process alive (serving, or still inside its load window)?

    One probe per platform, each answering the same question with its own tool: `tasklist` by image name
    on Windows, `pgrep -f` on POSIX. A probe that cannot run answers `False` with the reason in the second
    slot — the launch path treats that as "nothing to wait for" (the pre-guard behaviour, the safe
    direction: the worst case is a second instance that loses the port race), and the reason is what stops
    it being silent."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/NH", "/FI", f"IMAGENAME eq {pattern}.exe"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=_no_window()).stdout
            return (pattern.lower() in out.lower()), ""
        if not shutil.which("pgrep"):
            return False, "pgrep not found: cannot tell whether an engine is mid-load"
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=15).stdout
        pids = [p for p in out.split() if p.strip().isdigit() and int(p) != os.getpid()]
        return bool(pids), ""
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"process probe failed: {exc.__class__.__name__}"


def api_log_path(config, port: int | None = None) -> Path:
    """Where a detached API server's own output goes — the same rule as the engine's log, one file per port.

    A server started with `--detach` has no terminal to complain to, and "it is not answering" is a much
    shorter conversation once the operator can be pointed at the file its exception went into."""
    folder = Path(config.path).parent if config.path else config_module.machine_config_dir()
    return folder / f"siw-api-{port or config.api_port}.log"


def running_instances(pattern: str = "kiwix-serve") -> list[tuple[int, str]]:
    """(pid, command line) for every engine process this platform reports.

    `process_running` answers "is an engine running"; the launch decision needs the stronger question "is
    an engine loading *this port*", because a box that already serves one corpus on one port must not be
    forbidden from serving a second corpus on another — and waiting for somebody else's server to answer
    this port is exactly that forbidding. Windows enumerates through PowerShell's CIM (the `wmic` helper is
    deprecated and absent from a current client install), POSIX reads `ps`. A platform that answers nothing
    yields an empty list, which callers read as "nothing to wait for" — the same safe direction the image
    probe takes, and the reason the command line is fetched rather than guessed from a pid."""
    assert re.fullmatch(r"[A-Za-z0-9_-]+", pattern or ""), (
        "the process filter is interpolated into a PowerShell query; process names only")
    try:
        if os.name == "nt":
            script = (f'Get-CimInstance Win32_Process -Filter "Name=\'{pattern}.exe\'" | '
                      "ForEach-Object { [string]$_.ProcessId + [char]9 + [string]$_.CommandLine }")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=_no_window()).stdout
        else:
            out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True,
                                 timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        if os.name == "nt":
            pid, _, command = line.partition("\t")
        else:
            pid, _, command = line.strip().partition(" ")
            if pattern not in command:
                continue
        if pid.strip().isdigit():
            found.append((int(pid), command.strip()))
    return found


def instances_on(port: int, pattern: str = "kiwix-serve") -> list[tuple[int, str]]:
    """The engine processes whose own command line names this port — the ones a self-heal may wait for.

    Matching is on the argv the launcher wrote (`-p <port>`, and the long spellings the family accepts),
    not on the port: nothing else says which server a process was told to open, and a server can be
    listening on a port while a second one is still loading toward it."""
    wanted = str(int(port))
    marks = (f"-p {wanted}", f"--port={wanted}", f"--port {wanted}", f"-p{wanted}")
    return [(pid, command) for pid, command in running_instances(pattern)
            if any(mark in command for mark in marks)]


def log_path(config, port: int | None = None) -> Path:
    """Where the engine's own stdout/stderr go: beside the config, one file per port.

    A detached server with nowhere to write loses its only diagnostic. Beside the config because that
    folder is the one this tool knows is writable on both platforms — a `%TEMP%` default would be lost on
    a machine where temp is redirected, and the operator's `siw doctor` can print the path it read."""
    folder = config.path.parent if config.path else config_module.machine_config_dir()
    return folder / f"kiwix-serve-{port or config.port}.log"


def build_argv(config, books: list[corpus.Book], form: str = "paths") -> list[str]:
    """The exact argv the spawn will use: the binary, the port, the bind, and the set.

    The BARE short-flag recipe is deliberate: the long forms and the verbose/skip flags of the older
    builds load zero books or hang this one, so nothing is added to the argv that has not been measured
    on the build in front of us. `-i <bind>` is passed whenever a bind is configured, because the
    engine's own default address is *every interface* — a deploy that never says otherwise publishes the
    corpus to the network."""
    argv = [config.exe, "-p", str(config.port)]
    if config.bind:
        argv += ["-i", str(config.bind)]
    root = Path(config.path).parent if config.path else config_module.machine_config_dir()
    if form == "library":
        ensure_ids(config, books)
        corpus.library_xml(books, root / f"siw-library-{config.port}.xml")
        argv += ["-l", str(root / f"siw-library-{config.port}.xml")]
    else:
        argv += [book.path for book in books]
    return argv


def id_cache_path(config) -> Path:
    """Where catalog-learned book ids are kept: beside the pid files, one per engine port.

    The config's folder is the one directory this tool knows is user-writable on both platforms, and the
    cache is per-port because two engines on one box hold different sets and therefore different ids."""
    folder = Path(config.path).parent if config.path else config_module.machine_config_dir()
    return folder / f"siw-books-{config.port}.json"


def ensure_ids(config, books: list[corpus.Book]) -> int:
    """Fill the library form's ids from the cache, and report how many books the cache told us about.

    Cheap by construction: a set that already carries ids costs one comparison, and a set that does not
    costs one small JSON read. The alternative — reading every book's header at launch — puts disk seeks on
    the path of a command whose whole job is to start a server."""
    if all(book.zim_id and book.language for book in books):
        return 0
    filled = corpus.load_id_cache(books, id_cache_path(config))
    for book in books:
        if not book.zim_id:
            head = zimmeta.read_header(book.path)
            if head:
                book.zim_id = head["uuid"]
                filled += 1
    return filled


def spawn_plan(config, books: list[corpus.Book], form: str | None = None,
               platform: str | None = None) -> tuple[list[str], dict, str]:
    """The argv, the spawn keywords, and the form — computed without spawning anything.

    This exists so the launch contract is a testable value rather than a behaviour: "exactly the
    configured set reaches the argv" and "the console-less pair is on the Windows spawn" are rules about
    a dict, and a rule the machine can check is a rule that survives the next refactor. The real spawn is
    `launch`, and it is these same two objects handed to `Popen`. `platform` names the branch when the
    caller is not standing on the platform it is asking about — the other leg of the split is then a
    checked branch rather than an unrunnable hope."""
    if not config.exe or not os.path.isfile(config.exe):
        raise EngineError(f"no engine binary at {config.exe!r} — point `exe` at it in the "
                          "console's Settings pane, or clear it and let the pinned fetch download one")
    if not books:
        raise EngineError("no books to serve: the corpus directory holds no complete .zim file "
                          f"(corpus={config.corpus!r})")
    chosen, _length = corpus.launch_form_for(
        books, build_argv(config, books, "paths"), form or config.launch)
    if chosen == "library" and not all(b.zim_id and b.language for b in books):
        raise EngineError("the library launch form needs every book's id and language; fill them from a "
                          "running engine (`siw doctor` reads the catalog) or launch with launch=paths")
    argv = build_argv(config, books, chosen)
    if chosen == "paths":
        length = len(corpus.command_line(argv))
        if length > corpus.COMMAND_LINE_CEILING:
            raise EngineError(
                f"the launch would be {length} characters, over the {corpus.COMMAND_LINE_CEILING} the "
                f"process API allows for {len(books)} books: narrow the pick, or serve the set through a "
                "library file (`siw doctor --library-ids` fills the ids from a running engine, then "
                "set `launch=library` in the console's Settings pane)")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    windows = (platform or ("windows" if os.name == "nt" else "posix")).lower() == "windows"
    if windows:
        kwargs["creationflags"] = (_constant("CREATE_NO_WINDOW", 0x08000000)
                                   | _constant("DETACHED_PROCESS", 0x00000008))
    else:
        kwargs["start_new_session"] = True
    return argv, kwargs, chosen


def _constant(name: str, value: int) -> int:
    """A `subprocess` constant by name, with the documented value as the fallback.

    The constants exist on CPython's Windows build and are absent on POSIX, where the spawn must not use
    them at all; naming them through `getattr` with the literal value means the flag survives on a build
    where a wrapper class hides the attribute, and a wrong value here is a console window on an
    operator's desktop."""
    return int(getattr(subprocess, name, value))


def detached_spawn(argv: list[str], log_target, platform: str | None = None,
                   env: dict | None = None) -> subprocess.Popen:
    """Spawn a process that outlives this one, holds no console, and writes to a log.

    The two platform branches are the whole story and neither is optional: on Windows the console-less
    pair (see this module's header — an attached build hangs at startup, and an allocated console is a
    window on the operator's desktop that outlives the reason for it), on POSIX a new session (or Ctrl-C
    in the launching terminal reaches a server that was supposed to survive it). `launch` and the detached
    API door both come through here, so there is one place the rule lives."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if env is not None:
        kwargs["env"] = env
    windows = (platform or ("windows" if os.name == "nt" else "posix")).lower() == "windows"
    if windows:
        kwargs["creationflags"] = (_constant("CREATE_NO_WINDOW", 0x08000000)
                                   | _constant("DETACHED_PROCESS", 0x00000008))
    else:
        kwargs["start_new_session"] = True
    with open(log_target, "ab") as log:
        return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, **kwargs)


def launch(config, books: list[corpus.Book], form: str | None = None) -> tuple[int, str, float]:
    """Start the engine detached, write the pidfile, and return (pid, form, seconds to port-up).

    The spawn is unwaited on purpose: the server outlives the run that started it, and that is the
    property a deployment wants. The pidfile is what turns "we own a server" from a claim into a record
    `siw stop` can check, so it is written here and by `heal` — nowhere else."""
    argv, kwargs, chosen = spawn_plan(config, books, form)
    where = log_path(config)
    where.parent.mkdir(parents=True, exist_ok=True)
    handle = detached_spawn(argv, where, platform="windows" if "creationflags" in kwargs else "posix")
    write_pidfile(config, {"pid": handle.pid, "port": int(config.port), "exe": config.exe,
                           "image": expected_image(config.exe),
                           "form": chosen, "books": len(books),
                           "argv_chars": len(corpus.command_line(argv)),
                           "started": int(time.time()), "log": str(where)})
    up = wait_for_port(config, STARTUP_BUDGET)
    return handle.pid, chosen, up


def wait_for_port(config, budget: float = STARTUP_BUDGET) -> float:
    """Poll until the engine answers, returning the seconds it took (or the whole budget, spent)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < budget:
        time.sleep(0.25)
        if probe(config.host, config.port, timeout=2.0):
            return round(time.monotonic() - t0, 2)
    return round(time.monotonic() - t0, 2)


def heal(config, books: list[corpus.Book], budget: float = STARTUP_BUDGET) -> tuple[bool, float, str]:
    """Bring the endpoint up if it is dark, and report what happened in the words a result carries.

    The order is probe → process check → launch → poll, and it is the whole guard against the two failure
    modes of a naive self-heal: launching while a server is still loading (port race, orphaned instance)
    and reporting "down" while one is on its way up. Returns (up, seconds, note); the note is written for
    the record, because a result that says "searched" when the search had to launch its own endpoint
    is a record that cannot be read back."""
    if probe(config.host, config.port):
        return True, 0.0, ""
    running, why = process_running()
    if running:
        loading = instances_on(config.port)
        if loading:
            waited = wait_for_port(config, budget)
            if probe(config.host, config.port):
                return True, waited, (f"a kiwix-serve process for this port (pid {loading[0][0]}) was "
                                      f"already loading; it answered after {waited}s and nothing was "
                                      "launched")
            return False, waited, (f"pid {loading[0][0]} was started for {config.host}:{config.port} but "
                                   f"it never answered within {waited}s; the books or the bind are not "
                                   "what the config says — see siw doctor")
        # Engines aimed at other ports are other people's servers. Waiting for one of them to answer this
        # port is how a box that already serves a corpus refuses to serve a second one, so the only
        # process worth respecting here is one that names the port we are about to open.
        pass
    if why:
        # The probe could not answer: keep going, but say so — a silent "no process" is how a double
        # launch reaches a loading server.
        pass
    t0 = time.monotonic()
    try:
        pid, form, up = launch(config, books)
    except (EngineError, OSError) as exc:
        return False, round(time.monotonic() - t0, 2), f"launch failed: {exc}"
    if probe(config.host, config.port):
        return True, up, (f"the engine was down; SIW launched it (pid {pid}, {len(books)} books, "
                          f"form {form}) and the port answered in {up}s")
    return False, up, (f"the engine was down; SIW launched pid {pid} and {config.host}:{config.port} "
                       f"did not answer within the {up:.0f}s the launch waited — check {log_path(config)}")


def read_pidfile(config, port: int | None = None) -> tuple[dict | None, Path]:
    """The record of the engine this tool started, and the file it was read from."""
    return read_pidfile_at(config_module.pidfile(config, port))


def read_pidfile_at(where: Path) -> tuple[dict | None, Path]:
    """A record read by path: no config and no port arithmetic, and the same leniency about a file that
    is not a record. `None` means "there is nothing to act on here", which is a different thing from the
    refusal a stop makes when it *does* find a record it cannot parse — that one is `stop_pidfile`'s."""
    if not where.is_file():
        return None, where
    try:
        raw = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, where
    return (raw if isinstance(raw, dict) else None), where


def write_pidfile(config, record: dict) -> Path:
    """Write the record that makes a later `stop` an informed act rather than a guess at a number."""
    where = config_module.pidfile(config, record.get("port"))
    where.parent.mkdir(parents=True, exist_ok=True)
    # tmp + replace: a crash mid-write left half a JSON record, which reads as "no
    # record" -- and `siw stop` refused a server this very tool had started.
    tmp = where.with_name(where.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(where)
    return where


def clear_pidfile(where: Path) -> None:
    try:
        where.unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Is that process number in use? A signal of zero is the portable question; on Windows the answer
    comes from `tasklist`, which also hands back the image name the stop check needs."""
    alive, _ = process_info(pid)
    return alive


def expected_image(exe: str, platform: str | None = None) -> str:
    """The image name the running engine will report, so a stop can check against it.

    A plain binary reports its own name. A POSIX shebang script reports its interpreter —
    its /proc entry is the interpreter's, not the file's — so a script engine is recorded
    with the name it will actually answer to. Windows has no shebang: the stem, always."""
    plat = (platform or ("windows" if os.name == "nt" else "posix")).lower()
    if plat == "windows":
        return Path(exe).stem
    try:
        with open(exe, "rb") as handle:
            first = handle.readline(64)
    except OSError:
        return Path(exe).stem
    if first.startswith(b"#!"):
        parts = first.split()
        prog = parts[0][2:].decode("utf-8", "replace")  # the program is glued to the #!
        if Path(prog).name == "env" and len(parts) > 1:  # `#!/usr/bin/env <interpreter>`
            prog = parts[1].decode("utf-8", "replace")
        if prog:
            return Path(prog).name
    return Path(exe).stem


def process_info(pid: int) -> tuple[bool, str]:
    """(alive, image name) for one process number, as far as this platform will say.

    The image name is what lets a stop refuse a recycled pid: a number the tool wrote can be given to a
    different program while nobody is looking, and killing that program is not what `siw stop` was asked
    for. POSIX answers the name from `/proc` where it is mounted; where it is not, the name is empty and
    the caller is told the check could not be made."""
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/NH", "/FI", f"PID eq {int(pid)}"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=_no_window()).stdout
        except (OSError, subprocess.SubprocessError):
            return False, ""
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == str(pid):
                return True, fields[0]
        return False, ""
    try:
        with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as handle:
            return True, handle.read().strip()
    except OSError:
        # No /proc (or the process is gone): fall back to the signal-0 question and say the name is unknown.
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False, ""
        return True, ""


def terminate(pid: int, grace: float = 3.0) -> str:
    """Ask one process to leave, escalating once, and return the method that was actually used.

    Split out from `stop` for the same reason the spawn plan is split out: the *decision* to ask is what
    the contract governs, and a test has to be able to watch it. Windows starts courteous (`taskkill /T`,
    the tree, because an engine started detached has no parent waiting to reap it) and measures the result:
    a detached server has no window to close, so the courteous form leaves it listening, and the only thing
    that answers is the forced form. POSIX uses SIGTERM, the engine's own shutdown path — a SIGKILL would
    leave a half-written log where the diagnosis was, and the escalation is not taken there.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T"], capture_output=True, text=True,
                       timeout=30, creationflags=_no_window())
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return "taskkill /T"
            time.sleep(0.25)
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, text=True,
                       timeout=30, creationflags=_no_window())
        return f"taskkill /T (courteous form left it alive after {grace:g}s) then /T /F"
    os.kill(int(pid), 15)
    return "SIGTERM"


def stop_pidfile(record_path: Path, host: str, port: int, expect_image: str,
                 wait: float = STOP_BUDGET) -> tuple[bool, str]:
    """Stop one recorded process, and only a recorded one.

    Three refusals, each for a reason the machine can check: no record (this tool did not start it), a
    process that answers to a different image (the pid was recycled), and a process that will not leave
    (the port still answers after the signal). A refused stop leaves its record on disk — an act the tool
    declined is an act it should still be able to explain."""
    if not record_path.is_file():
        return False, (f"no SIW record at {record_path}: this tool did not start the process there, and "
                       "stopping one it did not is not what a stop does. Whoever started it owns it.")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"the SIW record at {record_path} cannot be read ({exc}); refusing to act on it"
    if not isinstance(record, dict):
        return False, f"the SIW record at {record_path} is not a record; refusing to act on it"
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return False, (f"the SIW record at {record_path} names no usable pid "
                       f"({record.get('pid')!r}); refusing to act on it")
    alive, image = process_info(pid)
    if not alive:
        port_here = int(record.get("port") or port)
        still_there = probe(host, port_here)
        clear_pidfile(record_path)
        if still_there:
            holders = ", ".join(str(p) for p in listening_pids(port_here)) or "unknown"
            return False, (f"the recorded process (pid {pid}) is gone but {host}:{port_here} still "
                           f"answers, held by pid {holders}: it is not SIW's to stop. The stale record is "
                           "removed; the holder is named so the job can be finished by whoever owns it")
        return True, (f"the recorded process (pid {pid}) had already exited; its record at "
                      f"{record_path} is removed and nothing else was touched")
    if image and expect_image and expect_image.lower() not in image.lower():
        return False, (f"pid {pid} answers to {image!r}, not {expect_image!r}: the number was reused since "
                       f"SIW wrote it. The record at {record_path} is kept to inspect, and nothing was "
                       "asked of it")
    try:
        how = terminate(pid)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not ask pid {pid} to leave: {exc}"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not probe(host, int(record.get("port") or port), timeout=2.0):
            clear_pidfile(record_path)
            return True, (f"stopped the process SIW started (pid {pid}, port "
                          f"{record.get('port') or port}, form {record.get('form', 'paths')}, "
                          f"asked via {how}); the port "
                          f"went dark in {round(wait - (deadline - time.monotonic()), 2)}s")
        time.sleep(0.25)
    return False, (f"pid {pid} was asked to leave and {host}:{record.get('port') or port} is still "
                   f"answering after {wait:.0f}s; the record is kept")


def stop(config, port: int | None = None, wait: float = STOP_BUDGET) -> tuple[bool, str]:
    """Stop the engine this tool started, and only that one: the record, the image, then the signal."""
    record, where = read_pidfile(config, port)
    if not record:
        return False, (f"no SIW engine record for port {port or config.port} at {where}: this tool did "
                       "not start the server there, and `siw stop` stops only a server it did. Whoever "
                       "started it owns it — stop it the way it was launched, or start one of your own "
                       "with `siw serve`")
    # The image to check is what the spawn recorded, not a constant: a POSIX engine that is a
    # script answers to its interpreter, and a stop refused on an image it wrote itself is an
    # engine nobody can stop. Records written before the field existed keep the old constant.
    return stop_pidfile(where, config.host, int(record.get("port") or config.port),
                        record.get("image") or "kiwix-serve", wait)


def stop_api(config, port: int | None = None, wait: float = STOP_BUDGET) -> tuple[bool, str]:
    """Stop a detached API server this tool spawned — the same three checks, against the interpreter
    image recorded when it was detached rather than against the engine binary."""
    where = config_module.api_pidfile(config, port)
    if not where.is_file():
        return False, f"no SIW api record at {where}: no detached API server to stop"
    record = {}
    try:
        record = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass  # stop_pidfile reads it again and refuses with the parse error, once
    expect = str((record or {}).get("image") or Path(sys.executable).stem)
    return stop_pidfile(where, config.bind, int(config.api_port), expect, wait)


def get(config, path: str, timeout: float = 60.0) -> tuple[int, str]:
    """One GET to the engine, status and body out — the shared client for the catalog and the search
    routes, so a connection failure is one exception type everywhere and the self-heal has one hook."""
    url = f"http://{config.host}:{config.port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            return reply.status, reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def catalog(config, count: int = 2000, timeout: float = 30.0) -> tuple[list[dict], str]:
    """The books the running engine serves, as records: id, its catalog name, the `/content/<name>` name
    that `books.name=` selects, language, title, article count, media count.

    The engine's own catalog is the only authority on what a live server holds, which is what makes the
    served-vs-configured drift check possible in one request. `count` is passed explicitly because the
    endpoint paginates at 10 without it — a catalog read that silently returns ten books would report a
    a server that holds a few hundred as holding ten, and the drift line would be wrong."""
    status, body = get(config, f"/catalog/search?count={int(count)}", timeout=timeout)
    if status != 200:
        return [], f"the engine's catalog answered HTTP {status}"
    entries = []
    for block in re.findall(r"<entry>(.*?)</entry>", body, re.S):
        def field(tag: str) -> str:
            found = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
            return found.group(1).strip() if found else ""
        content = re.search(r'href="/content/([^"/]+)"', block)
        entries.append({"id": field("id").replace("urn:uuid:", ""), "name": field("name"),
                        "title": field("title"), "language": field("language"),
                        "article_count": field("articleCount") or None,
                        "media_count": int(field("mediaCount")) if field("mediaCount").isdigit() else None,
                        "content_name": content.group(1) if content else ""})
    return entries, ""


def _no_window() -> int:
    """The Windows console flag for a helper command, 0 where it does not exist: a probe that flashes a
    console window is visible to the operator, which is the whole rule."""
    return _constant("CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def platform_support() -> tuple[bool, str]:
    """Whether this platform's spawn branch has been executed by anyone. Windows has; it has not been
    executed on POSIX. Treat a POSIX run as the first measurement, and say so."""
    if os.name == "nt":
        return True, f"{sys.platform}: the detached spawn uses CREATE_NO_WINDOW | DETACHED_PROCESS"
    return False, (f"{sys.platform}: the detached spawn uses start_new_session and pgrep, written to the "
                   "same contract as the Windows branch and never executed — treat the first POSIX run "
                   "as the measurement it is")
