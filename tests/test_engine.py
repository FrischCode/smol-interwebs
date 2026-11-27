"""The launch contract, as a value: exactly the configured set reaches the argv, and the spawn carries the
flags that keep a headless server off the operator's desktop.

These tests never spawn. They read `spawn_plan`'s argv and keyword dict — which is what `launch` hands to
`Popen` — so the rules that turn a hung-console incident into a machine-held invariant are checked in
process, on every run, on whichever platform the battery happens to stand on. The `platform` argument is
that: the branch a real run picks is the platform it is on, and the branch the other platform takes is
asserted here so it is at least a checked branch and not an unrunnable hope.
"""
import os

import pytest

from siw import config, corpus, engine

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


@pytest.fixture
def box(tmp_path):
    """A corpus of three books, a binary that exists, and a config pointing at both — plus the pidfile
    folder beside them, which is what a stop is allowed to read."""
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()
    for name in ("one_2026-10.zim", "two_2026-10.zim", "three_2026-10.zim"):
        (corpus_dir / name).write_bytes(b"")
    exe = tmp_path / "bin" / "kiwix-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    conf = config.Config({"corpus": str(corpus_dir), "exe": str(exe), "port": 18099,
                          "api_port": 18098, "bind": "127.0.0.1"}, path=tmp_path / "config.json")
    return conf, corpus.list_books(corpus_dir)


def test_the_argv_carries_exactly_the_configured_set(box):
    """The pick is only real if it reaches the launch: every selected book and no other file from
    the directory appears in the argv, in the pick's order.

    The self-heal that launched every `*.zim` in the directory made a picked subset cosmetic; this is the
    test that catches that regression in either direction — a book dropped from the argv, or a book the
    operator never selected served anyway."""
    conf, books = box
    picked = corpus.select(books, ["two_2026-10", "one"])
    argv, _kwargs, form = engine.spawn_plan(conf, picked)
    assert form == "paths"
    served_paths = [a for a in argv if a.endswith(".zim")]
    assert [os.path.basename(p) for p in served_paths] == ["two_2026-10.zim", "one_2026-10.zim"]
    assert argv[0] == conf.exe
    assert argv[1:3] == ["-p", "18099"]


def test_the_bind_is_passed_because_the_engines_default_is_every_interface(box):
    """The engine's own default address is every interface, so a launch that never says otherwise
    publishes the corpus to the network. The bind is not an optional nicety: it is on the argv, and the
    only way a fresh deploy inherits loopback is by being launched with it."""
    conf, books = box
    argv, _kwargs, _form = engine.spawn_plan(conf, books)
    assert argv[argv.index("-i") + 1] == "127.0.0.1"
    conf.set("bind", "0.0.0.0")
    argv, _kwargs, _form = engine.spawn_plan(conf, books)
    assert argv[argv.index("-i") + 1] == "0.0.0.0"


def test_windows_spawn_carries_the_console_less_pair(box):
    """`CREATE_NO_WINDOW | DETACHED_PROCESS` is the rule that came out of a server that hung at startup
    attached to any console, and out of an operator who killed the console window they mistook for a stuck
    process. `CREATE_NO_WINDOW` alone is not enough: the pair, on every Windows launch, always.

    Asserted regardless of the platform the battery runs on, because the invariant is not conditional and
    a test that only checks the host's branch leaves the other one unrun."""
    conf, books = box
    _argv, kwargs, _form = engine.spawn_plan(conf, books, platform="windows")
    flags = kwargs.get("creationflags", 0)
    assert flags & CREATE_NO_WINDOW and flags & DETACHED_PROCESS
    assert kwargs.get("start_new_session") is not True, "the POSIX detach on a Windows spawn is a no-op"


def test_posix_spawn_starts_its_own_session(box):
    """On POSIX the detach is a session, not a window flag: without `start_new_session` a healed server
    stays in the launching terminal's process group and Ctrl-C reaches it. `tasklist` does not exist there,
    so the process check must be `pgrep` — which is `engine.process_running`'s own branch, and it is
    exercised by name below."""
    conf, books = box
    _argv, kwargs, _form = engine.spawn_plan(conf, books, platform="posix")
    assert kwargs.get("start_new_session") is True
    assert "creationflags" not in kwargs, "a Windows flag on the POSIX spawn raises in CPython"


def test_an_explicit_launch_form_reaches_the_argv_and_the_record(box, tmp_path):
    """The library form passes one XML path instead of a book per argument — the only form that survives a
    large picked set — and the record says which form is live, because "which set is served, and how" is a
    question the operator is otherwise left to guess."""
    conf, books = box
    for book in books:
        book.zim_id, book.language = f"00000000-0000-0000-0000-00000000000{books.index(book)}", "eng"
    argv, _kwargs, form = engine.spawn_plan(conf, books, form="library")
    assert form == "library"
    assert argv[argv.index("-l") + 1].endswith("siw-library-18099.xml")
    assert not any(a.endswith(".zim") for a in argv)
    library = next(p for p in tmp_path.iterdir() if p.name.endswith(".xml"))
    text = library.read_text(encoding="utf-8")
    assert text.count("<book ") == 3 and 'language="eng"' in text


def test_the_library_form_refuses_a_book_whose_id_it_does_not_have(box):
    """Inventing an id is worse than refusing: the engine answers 500 to every search against a library it
    cannot resolve, far from the config line that caused it. The refusal names the fix."""
    conf, books = box
    books[0].zim_id, books[0].language = "aaaa0000-0000-0000-0000-000000000000", "eng"
    with pytest.raises(engine.EngineError) as caught:
        engine.spawn_plan(conf, books, form="library")
    assert "id and language" in str(caught.value)


def test_auto_prefers_paths_and_switches_only_when_the_command_line_demands_it(box, monkeypatch):
    """`auto` is the measured form by default; the library form arrives when the command line approaches
    the CreateProcess ceiling and the ids are known to fill it. A corpus that needs neither is served the
    way this engine family was measured serving."""
    conf, books = box
    assert engine.spawn_plan(conf, books)[2] == "paths"
    fake = [f"{'x' * 90}.zim" for _ in range(400)]
    monkeypatch.setattr(corpus, "list_books", lambda _c: [corpus.Book(p) for p in fake])
    long_books = [corpus.Book(p) for p in fake]
    for book in long_books:
        book.zim_id, book.language = "aaaa0000-0000-0000-0000-000000000000", "eng"
    assert engine.spawn_plan(conf, long_books)[2] == "library"


def test_no_binary_and_no_books_are_refused_before_anything_spawns(box, tmp_path):
    """Two refusals that save a confusing engine failure: no binary at the configured path, and a corpus
    directory holding no complete book. Both name the config field to fix."""
    conf, books = box
    missing = config.Config({"corpus": conf.corpus, "exe": str(tmp_path / "nope.exe"),
                             "port": conf.port}, path=conf.path)
    with pytest.raises(engine.EngineError) as caught:
        engine.spawn_plan(missing, books)
    assert "no engine binary" in str(caught.value)
    with pytest.raises(engine.EngineError) as caught:
        engine.spawn_plan(conf, [])
    assert "no books to serve" in str(caught.value)


def test_the_pidfile_is_the_only_thing_a_stop_may_act_on(box):
    """No record, no stop: a server the tool did not start belongs to whoever started it, and the answer
    is the sentence that says so, not a kill. This is what keeps a helper on a shared box from reaching
    for a process number it did not issue."""
    conf, _books = box
    stopped, message = engine.stop(conf)
    assert not stopped
    assert "did not start" in message or "no SIW engine record" in message
    assert str(conf.path.parent) in message, "the refusal names the record it looked for"


def test_a_stop_writes_nothing_and_a_record_survives_a_failed_stop(box):
    """A refused stop leaves its record on disk: the operator needs to see what the tool declined to
    touch, and a refusal that deletes its own evidence is a refusal nobody can audit."""
    conf, _books = box
    where = engine.write_pidfile(conf, {"pid": 999999, "port": conf.port, "exe": conf.exe,
                                        "form": "paths", "books": 3})
    assert where.is_file()
    record, _path = engine.read_pidfile(conf)
    assert record and record["books"] == 3
    assert where.is_file(), "a stop that refused must not have cleared the record"


def test_a_recycled_pid_is_refused_and_kept(box, monkeypatch):
    """A process number outlives its process: the pid this tool wrote can belong to something else by the
    time anyone types `siw stop`, and killing that would be an unasked-for act. The check is the image
    name, and the record stays put."""
    conf, _books = box
    engine.write_pidfile(conf, {"pid": 4242, "port": conf.port, "exe": conf.exe, "form": "paths"})
    monkeypatch.setattr(engine, "process_info", lambda _pid: (True, "notepad.exe"))
    asked = []
    monkeypatch.setattr(engine, "terminate", lambda pid: asked.append(pid) or "asked")
    stopped, message = engine.stop(conf)
    assert not stopped and "reused" in message
    assert not asked, "nothing may be asked of a process SIW has not identified as the engine"


def test_expected_image_records_what_the_process_will_report(box):
    """A stop can only check against the name the running process will actually report: a plain
    binary its own name, a POSIX shebang script its interpreter (the /proc entry is the
    interpreter's). The helper answers per platform, and the no-parameter call matches the
    native branch on whichever platform the suite is running on."""
    conf, _books = box
    base = conf.path.parent
    via_env = base / "eng-a"
    via_env.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert engine.expected_image(str(via_env), platform="posix") == "python3"
    direct = base / "eng-b"
    direct.write_text("#!/usr/bin/python3.11\n", encoding="utf-8")
    assert engine.expected_image(str(direct), platform="posix") == "python3.11"
    plain = base / "kiwix-serve"
    plain.write_bytes(b"\x7fELFfake")
    assert engine.expected_image(str(plain), platform="posix") == "kiwix-serve"
    assert engine.expected_image(str(base / "gone"), platform="posix") == "gone"
    assert engine.expected_image(str(conf.exe), platform="windows") == "kiwix-serve"
    assert engine.expected_image(str(conf.exe)) == "kiwix-serve"


def test_a_stop_checks_the_image_the_spawn_recorded_not_a_constant(box, monkeypatch):
    """The image check uses what the record says, not a hardcoded binary name: a spawn of a
    script engine records the interpreter, and its stop proceeds. (First POSIX measurement:
    the hardcoded name made a script engine refuse its own stop and survive it.) A record
    predating the field keeps the old constant — old record, old behaviour."""
    conf, _books = box
    engine.write_pidfile(conf, {"pid": 5151, "port": conf.port, "exe": conf.exe,
                                "image": "python3", "form": "paths"})
    monkeypatch.setattr(engine, "process_info", lambda _pid: (True, "python3"))
    asked = []
    monkeypatch.setattr(engine, "terminate", lambda pid: asked.append(pid) or "asked")
    monkeypatch.setattr(engine, "probe", lambda *_a, **_k: False)
    stopped, message = engine.stop(conf)
    assert stopped and asked == [5151]
    engine.write_pidfile(conf, {"pid": 5252, "port": conf.port, "exe": conf.exe, "form": "paths"})
    asked.clear()
    stopped, message = engine.stop(conf)
    assert not stopped and "reused" in message and not asked


def test_a_stop_of_a_gone_engine_clears_the_record_only(box, monkeypatch):
    """An engine that exited on its own leaves a stale record; the stop reports it gone and clears it, and
    still asks nothing of a process number."""
    conf, _books = box
    engine.write_pidfile(conf, {"pid": 4343, "port": conf.port, "exe": conf.exe})
    monkeypatch.setattr(engine, "process_info", lambda _pid: (False, ""))
    monkeypatch.setattr(engine, "probe", lambda *_a, **_k: False)
    stopped, message = engine.stop(conf)
    assert stopped and "already exited" in message
    assert engine.read_pidfile(conf)[0] is None


def test_the_port_record_and_the_log_are_one_per_port(box):
    """Two engines on one box — the deployment's and a test's — must not share a pidfile or a log, and the
    record names the port it belongs to so a stop can check it is stopping the one it means."""
    conf, _books = box
    assert engine.log_path(conf).name == "kiwix-serve-18099.log"
    assert engine.log_path(conf, 18101).name == "kiwix-serve-18101.log"
    first = config.pidfile(conf)
    second = config.pidfile(conf, 18101)
    assert first != second and first.parent == conf.path.parent


def test_platform_support_names_the_leg_that_has_never_been_run():
    """The POSIX branch of the spawn is written and unexecuted. A tool that says "Windows and POSIX"
    without saying which one it has measured is a tool that will be trusted on the wrong machine."""
    supported, note = engine.platform_support()
    if os.name == "nt":
        assert supported and "CREATE_NO_WINDOW" in note
    else:
        assert not supported and "never executed" in note


def test_stop_refuses_a_record_naming_a_non_numeric_pid(tmp_path):
    """m13: an integer coercion in the middle of `stop` turned a half-written pidfile
    into a ValueError escaping as a 500. The record is refused by name instead."""
    from siw import engine as engine_mod
    where = tmp_path / "siw-engine.pid"
    where.write_text('{"pid": "not-a-number", "port": 18099}', encoding="utf-8")
    ok, note = engine_mod.stop_pidfile(where, "127.0.0.1", 18099, "kiwix-serve")
    assert ok is False and "no usable pid" in note


def test_the_pidfile_write_is_atomic(tmp_path, monkeypatch):
    """m13: the record is written tmp-then-replace, so a crash mid-write can no longer
    leave half a JSON file that reads as `no record` to `siw stop`."""
    from siw import config as config_module
    from siw import engine as engine_mod
    cfg = config_module.Config({"port": 18099}, path=tmp_path / "siw.json")
    where = engine_mod.write_pidfile(cfg, {"pid": 1234, "port": 18099})
    assert where.is_file()
    assert not where.with_name(where.name + ".tmp").exists()
    assert engine_mod.read_pidfile_at(where)[0]["pid"] == 1234


def test_the_catalog_parser_carries_the_media_count(box, monkeypatch):
    """mediaCount is the engine's own word on a book's content (text, or text and images); the
    parsed record carries it so the console's media column is fed from the live catalog. An
    entry without mediaCount (an older engine) is None, not 0 — absence is not zero."""
    conf, _books = box
    xml = (
        "<feed><entry>"
        "<id>urn:uuid:aaaa-bbbb</id><title>Picture book</title><language>eng</language>"
        "<name>with_media</name><articleCount>10</articleCount><mediaCount>300</mediaCount>"
        '<link type="text/html" href="/content/with_media" />'
        "</entry>"
        "<entry>"
        "<id>urn:uuid:cccc-dddd</id><title>Plain book</title><language>eng</language>"
        "<name>plain</name><articleCount>5</articleCount><mediaCount>0</mediaCount>"
        '<link type="text/html" href="/content/plain" />'
        "</entry>"
        "<entry>"
        "<id>urn:uuid:eeee-ffff</id><title>Old book</title><language>eng</language>"
        "<name>oldstyle</name><articleCount>3</articleCount>"
        '<link type="text/html" href="/content/oldstyle" />'
        "</entry></feed>"
    )
    monkeypatch.setattr(engine, "get", lambda cfg, url, timeout=30.0: (200, xml))
    entries, problem = engine.catalog(conf)
    assert problem == ""
    by_name = {e["name"]: e for e in entries}
    assert by_name["with_media"]["media_count"] == 300
    assert by_name["plain"]["media_count"] == 0
    assert by_name["oldstyle"]["media_count"] is None
