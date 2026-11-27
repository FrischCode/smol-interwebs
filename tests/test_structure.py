"""Structural rules the project promises. These run against the tree, not against behaviour, so they hold
when there is almost nothing in it yet — and they fail the moment a promise is traded away for convenience.

The claims under test are the ones the README makes: the core is stdlib-only, the battery is hermetic, and
no file carries a decision somebody meant to write down later.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "siw"
TESTS = ROOT / "tests"
# This file lists the strings it forbids, so scanning it would report its own patterns.
SELF = pathlib.Path(__file__).resolve()
# The folders that are not shipped content, and the account names that are placeholders rather
# than people: a path whose user component is one of these says "some machine", one that is not
# says which one.
NON_SHIPPED = {".git", "__pycache__", ".venv", "scratch"}
PLACEHOLDER_ACCOUNTS = {"example", "user", "you", "someone", "%username%", "$user", "<user>"}


def _sources(folder, skip_self=False):
    found = list(folder.rglob("*.py"))
    return [p for p in found if not (skip_self and p.resolve() == SELF)]


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_core_imports_stdlib_and_siw_only():
    """A dependency is a decision, not a convenience: nothing outside the standard library may be
    imported by the core, and nothing may reach outside the package.

    The tool is sold as deployable on a machine that has a corpus and a Python. Every import of a third
    package is a machine where `uv pip install -e .` reaches for an index and fails, so the rule is the
    product claim, and this is the test that keeps the claim from rotting one convenience at a time."""
    allowed = set(sys.stdlib_module_names) | {"siw"}
    for path in _sources(SRC):
        for module in _imports(path):
            assert module in allowed, f"{path.relative_to(ROOT)} imports {module!r}"


def test_no_module_bypasses_the_import_rule():
    """`__import__` and `importlib` are the same door with a string on it: the scan above reads the
    syntax tree, and a dynamic import is a dependency the tree does not show.

    Naming the module and its reason is the honest alternative; a runtime dependency is allowed exactly
    when `pyproject.toml` carries the decision, which is where a reader of the packaging would look."""
    for path in _sources(SRC):
        text = path.read_text(encoding="utf-8")
        for marker in ("__import__", "importlib"):
            assert marker not in text, f"{path.relative_to(ROOT)} imports dynamically: {marker}"


def test_the_battery_has_no_skips_and_no_xfails():
    """A test that cannot fail is not in the battery, and a known-red test is a lie about the state of
    the tree. An out-of-band check that runs only against a real server is a script, run and printed —
    not a test that pretends."""
    for path in _sources(TESTS, skip_self=True):
        text = path.read_text(encoding="utf-8")
        for marker in ("skipif", "xfail", "pytest.skip"):
            assert marker not in text, f"{path.name} contains {marker!r}"


def test_the_battery_runs_in_process():
    """The battery is fast and deterministic by construction: no child processes, no sockets, no real
    clocks. Importing a module to reach its exception type is allowed — spawning through it is not.

    What needs a process boundary is the live check, and the live check prints what it measured. Keeping
    the two apart is what lets `pytest` stay a second and the server-behaviour claims stay measurements."""
    forbidden = {("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
                 ("subprocess", "check_output"), ("os", "system"), ("os", "popen"),
                 ("socket", "socket"), ("socket", "create_connection"), ("threading", "Thread"),
                 ("urllib.request", "urlopen")}
    for path in _sources(TESTS):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name):
                pair = (node.func.value.id, node.func.attr)
                assert pair not in forbidden, f"{path.name} calls {pair[0]}.{pair[1]}()"


def test_no_file_is_left_for_the_collector_to_close():
    """`open(...)` without a `with` hands the handle to the garbage collector: on CPython that means "when
    this stops being referenced", on another implementation "whenever". For a short read nobody notices,
    which is how the shape spreads; around a write, or when one run touches the same file twice, it is a
    locked file on Windows and an error with no line in it.
    """
    offenders = []
    for path in _sources(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        held = {id(item.context_expr) for node in ast.walk(tree) if isinstance(node, ast.With)
                for item in node.items if isinstance(item.context_expr, ast.Call)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in held:
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"opened without a context manager: {', '.join(sorted(offenders))}"


def test_no_marker_debt_in_the_tree():
    """A TODO is a decision deferred without a reason. Either it is written down or it is done."""
    for path in list(_sources(SRC)) + list(_sources(TESTS, skip_self=True)):
        text = path.read_text(encoding="utf-8")
        for marker in ("TODO", "FIXME", "XXX"):
            assert marker not in text, f"{path.relative_to(ROOT)} carries a {marker}"


def _shipped_text_files():
    """Every text file the tree ships: the source, the page, its mock, the docs, the launchers.

    A machine fact does not respect the package boundary — the one that mattered most sat in the
    console's mock state — so the scan is the whole tree, less the folders that are not content and
    the files with no text in them."""
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix == ".pyc" or path.resolve() == SELF:
            continue
        if any(part in NON_SHIPPED for part in path.parts):
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        yield path


def test_no_module_names_the_box_it_was_developed_on():
    """A path to one machine's drive letter is a deploy that fails quietly: the default that only
    exists there is the default a fresh machine cannot have.

    The corpus directory, the binary and the port are configuration — the console asks for them, the
    config file records them, and the shipped tree holds generic defaults and no local absolutes.
    Three families count as a machine fact, and the scan runs over every text file the tree ships,
    not just the core, because a mock state is content a reader trusts:

    * **a drive letter that is not `C:`** — `C:` is the one drive a Windows box necessarily has, so a
      `C:` path in an example teaches the shape without naming a volume somebody mounted here;
    * **an account name inside a path** — a `Users` component that is not a placeholder says who
      wrote this, and a mock that prints a real home directory prints the developer;
    * **a fingerprint of the corpus this was built against** — a book count, or a figure measured off
      one operator's shelf. "Measured on a large corpus" carries the same decision; the number does
      not travel, because the reader's corpus is not this one.

    What is not a leak stays: `%LOCALAPPDATA%` and `Path.home()` computed at runtime are
    portability, and the loopback default, the pinned binary's hash and its measured properties are
    the product — facts about the artifact, which is the one machine this does describe."""
    import re
    drive = re.compile(r"(?<![A-Za-z0-9])(?!C:|c:)[A-Za-z]:[\\/]")
    account = re.compile(r"Users[\\/]+([A-Za-z0-9_.%$<>-]+)")
    fingerprints = ('178-book', '178 books', '"books": 178', '10,475', 'this box')
    offenders = []
    for path in _shipped_text_files():
        where = path.relative_to(ROOT)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if drive.search(line):
                offenders.append(f"{where}:{n}: a drive letter that is not C:")
            named = account.search(line)
            if named and named.group(1).lower() not in PLACEHOLDER_ACCOUNTS:
                offenders.append(f"{where}:{n}: an account name in a path")
            for mark in fingerprints:
                if mark in line:
                    offenders.append(f"{where}:{n}: the development corpus, quoted ({mark!r})")
    assert not offenders, "machine- or corpus-specific content shipped: " + "; ".join(offenders)


def test_text_files_are_lf_and_batch_files_are_crlf():
    """Line endings are not cosmetic here: a `.bat` file with LF can be misparsed by cmd, and a Python
    file with CRLF read back through a diff tool shows every line changed. The rule is enforced rather than hoped for — a `.bat`
    written on Windows by this tool's front door has to survive being checked out.
    """
    offenders = []
    for path in [p for folder in (SRC, TESTS) for p in folder.rglob("*") if p.is_file()]:
        if path.suffix in {".pyc"} or path.parent.name == "__pycache__":
            continue
        raw = path.read_bytes()
        if path.suffix == ".bat":
            if b"\r\n" not in raw:
                offenders.append(f"{path.relative_to(ROOT)}: a .bat without CRLF")
        elif b"\r\n" in raw:
            offenders.append(f"{path.relative_to(ROOT)}: CRLF in a text file")
    for path in (ROOT / "pyproject.toml", ROOT / "README.md"):
        if path.is_file() and b"\r\n" in path.read_bytes():
            offenders.append(f"{path.name}: CRLF in a text file")
    for path in sorted(ROOT.glob("*.bat")):
        if b"\r\n" not in path.read_bytes():
            offenders.append(f"{path.name}: a .bat without CRLF")
    for path in sorted(ROOT.glob("*.sh")):
        if b"\r\n" in path.read_bytes():
            offenders.append(f"{path.name}: CRLF in a shell script")
    assert not offenders, "wrong line endings: " + "; ".join(sorted(offenders))


def test_the_dated_facts_live_in_the_findings_file():
    """Docstrings carry universal reasons; measurements carry dates. A dated claim in a docstring is a
    fact nobody will re-run and a reader cannot tell is stale, so a dated claim has to say its method and live where measurements live.

    The scan is for a date in a source docstring, not for the word "measured": the reason a build needs a
    console-less spawn is universal, and the date the need was observed belongs with the measurement."""
    import re
    dated = re.compile(r"20\d\d-\d\d-\d\d")
    offenders = []
    for path in _sources(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node) or ""
                if dated.search(doc):
                    name = getattr(node, "name", "<module>")
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, "dated prose in a docstring (measurements belong with their method): " + ", ".join(
        sorted(offenders))
