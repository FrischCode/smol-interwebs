"""Put the repo root on the path so `import siw` resolves to this tree, and make the battery mean the
same thing on every machine.

Two ways a battery stops being portable. The first is an ambient environment: a run started inside a
deployment exports `SIW_CORPUS`, `SIW_PORT` and friends, and an assertion about a default would then
read that machine's corpus and that machine's port instead of the shipped values. The battery's
contract is the same in every shell, so the `SIW_` values are cleared before any test runs, and a
monkeypatch that sets one restores the cleared state after it.

The second is a config file on the machine: the user-level `%LOCALAPPDATA%\\siw\\config.json` is exactly
the file `resolve()` reads when nothing else is named, so an unguarded test would read — and, worse,
could write — the operator's real settings. `SIW_CONFIG` is pinned at a path inside `tmp_path` that does
not exist: `resolve()` then resolves defaults, and a test that wants a file points it at its own.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _name in [key for key in os.environ if key.startswith("SIW_")]:
    del os.environ[_name]
os.environ["SIW_CONFIG"] = str(ROOT / "tests" / "_no_config_for_the_battery.json")
