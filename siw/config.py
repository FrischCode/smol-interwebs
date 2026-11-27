"""siw.config -- the one file this machine reads: which corpus, which books, which endpoints.

JSON, not dotenv: `json.load` and `JSON.parse` are both free, so an adapter written in any language
reads the same file without a parser, while a `.env` needs one written by hand.

The search order is explicit and the resolved path is returned with the values, so a report can name
the file it read:

    --config PATH  >  $SIW_CONFIG  >  ./siw.json  >  <machine config dir>/config.json  >  defaults

and the environment overrides a *file's* value field by field, so a shell export or a flag always wins.
The machine config dir is `%LOCALAPPDATA%\\siw` on Windows, `$XDG_CONFIG_HOME/siw` else `~/.config/siw`
elsewhere: a per-user machine setting is machine state, not tree content, so it is not written next to
the code and does not travel with a clone.

The field set is **closed**: an unknown key is a named refusal at load, never a silent drop. A typo in a
field name would otherwise read as "the operator never set that". `version` is the file's own shape stamp; a file from a future shape is refused with the
remedy rather than half-read.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

VERSION = 1

# Every field, its default, and the type it must hold. `selected=None` means "the whole corpus": the
# pick is the operator's act, and the tool never narrows a corpus nobody narrowed.
FIELDS: dict[str, tuple[Any, type | tuple[type, ...]]] = {
    "corpus": (None, str),                     # the directory holding the *.zim files
    "selected": (None, list),                  # book names (file stems); None = every book
    "port": (18099, int),                      # the engine: what kiwix-serve listens on
    "api_port": (7662, int),                   # SIW's own HTTP surface (the console and the API, one port)
    "host": ("127.0.0.1", str),                # the address SIW reaches the engine at
    "bind": ("127.0.0.1", str),                # the address SIW's own API listens on
    "username": ("", str),                     # console credentials: wanted whenever bind is not loopback
    "password": ("", str),                     # empty = the console answers without credentials (loopback only)
    "exe": (None, str),                        # the kiwix-serve binary the engine launches
    "launch": ("auto", str),                   # auto | paths | library: how the set reaches the engine
    "open_browser": (True, bool),              # a plain serve opens the console in a browser; false keeps the tab
    "version": (VERSION, int),
}

# The environment layer, above the file: name -> field. One value can be overridden by exporting
# its name.
ENV_NAMES = {
    "SIW_CORPUS": "corpus",
    "SIW_SELECTED": "selected",
    "SIW_PORT": "port",
    "SIW_API_PORT": "api_port",
    "SIW_HOST": "host",
    "SIW_BIND": "bind",
    "SIW_USERNAME": "username",
    "SIW_PASSWORD": "password",
    "SIW_EXE": "exe",
    "SIW_LAUNCH": "launch",
    "SIW_OPEN_BROWSER": "open_browser",
}

LAUNCH_FORMS = ("auto", "paths", "library")


class ConfigError(Exception):
    """A config file that cannot be honoured: unknown key, wrong type, wrong version, unreadable.

    The message always names the file and the field, because the cost of a vague config error is an
    operator editing the wrong line while the tool reads a different file."""


def machine_config_dir() -> Path:
    """Where this user's config lives when nothing else was named: `%LOCALAPPDATA%\\siw`, or XDG, or
    `~/.config/siw`. Deliberately not the repo: a clone must not carry this machine's corpus paths."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "siw" if base else Path.home() / ".siw"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "siw"


def search_order(explicit: str | None = None) -> list[Path]:
    """The candidates, best first. `explicit` (the `--config` flag) replaces the whole chain; a named
    file that does not exist is an error rather than a reason to fall through to a guess."""
    if explicit:
        return [Path(explicit).expanduser()]
    out = []
    env = os.environ.get("SIW_CONFIG")
    if env:
        out.append(Path(env).expanduser())
    out.append(Path.cwd() / "siw.json")
    out.append(machine_config_dir() / "config.json")
    return out


class Config:
    """The resolved settings, plus the path they were read from (None when they are all defaults).

    Values are plain (str/int/list/None) so the same dict can be written back, dumped by the API, and
    compared in a test without a dataclass on the wire."""

    def __init__(self, values: dict | None = None, path: Path | None = None,
                 sources: dict | None = None):
        merged = {name: default for name, (default, _kind) in FIELDS.items()}
        merged.update(values or {})
        self.values = merged
        self.path = path
        # Which layer last set each field ("default", "file", "env"), so a report can say where a value
        # came from.
        self.sources = sources or {name: "default" for name in FIELDS}

    def __getattr__(self, name: str):
        if name in FIELDS:
            return self.values[name]
        raise AttributeError(name)

    def __repr__(self) -> str:
        where = str(self.path) if self.path else "<defaults>"
        return f"Config({where}, corpus={self.values['corpus']!r}, port={self.values['port']})"

    def as_dict(self) -> dict:
        """Every field, in the declared order — the shape of the file on disk, and of `config show`."""
        return {name: self.values[name] for name in FIELDS}

    def set(self, name: str, value: Any, source: str = "flag") -> "Config":
        """One field replaced, validated, returning the same object for chaining.

        Validation is the loader's, so no writer can leave a file the next run refuses."""
        self.values[name] = _checked(name, value, "set")
        self.sources[name] = source
        return self

    def selected_books(self) -> list[str] | None:
        """The picked set, normalised: None stays None (whole corpus), a list is trimmed of blanks."""
        if self.values["selected"] is None:
            return None
        return [str(b).strip() for b in self.values["selected"] if str(b).strip()]


def _checked(name: str, value: Any, where: str) -> Any:
    """One field against its declared type, refusing by name and by what it got instead."""
    if name not in FIELDS:
        raise ConfigError(f"unknown config field {name!r} (in {where}); known: {', '.join(FIELDS)}")
    _default, kind = FIELDS[name]
    if value is None and name in ("corpus", "selected", "exe"):
        return None
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"{name} wants an integer, {where} gave a boolean")
    if not isinstance(value, kind):
        wanted = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
        raise ConfigError(f"{name} wants {wanted}, {where} gave {type(value).__name__}")
    if name in ("port", "api_port") and not (1 <= int(value) <= 65535):
        raise ConfigError(f"{name} wants a port between 1 and 65535, {where} gave {value}")
    if name == "launch" and value not in LAUNCH_FORMS:
        raise ConfigError(f"launch wants one of {', '.join(LAUNCH_FORMS)}, {where} gave {value!r}")
    return value


def parse_value(raw: str, field: str | None = None) -> Any:
    """A CLI/env string to a field value: `null` to nothing, digits to an int, `a,b` to a list only for
    the one list field. Deliberately narrow — a config value is not a scripting language.

    `field` is what makes a list spellable: `selected` holds book names, and one shell argument cannot be
    a list without a separator, so `a,b` is it. A JSON array is accepted as well, because the caller most
    likely to set this field from a script will write JSON. Other fields are never lists, and a value for
    them stays the string it was — `port=[80]` is a string a moment later refused as not an int."""
    text = raw.strip()
    if text.lower() in ("null", "none", ""):
        return None
    if field == "selected":
        if text.startswith(("[", "{")):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"selected is not a list I can read: {exc}") from exc
            if not isinstance(value, list):
                raise ConfigError(f"selected wants a list of book names, this is a {type(value).__name__}")
            return value
        return [part.strip() for part in text.split(",") if part.strip()]
    if field:
        _default, kind = FIELDS.get(field, (None, None))
        if kind is bool:
            low = text.lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
            raise ConfigError(f"{field} wants true or false, the value {raw!r} is neither")
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def load(path: Path) -> tuple[dict, str]:
    """One file's fields, with the layer name the findings print. The version is checked first: a file
    written by a future SIW is refused whole, not half-read into today's defaults."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} holds a {type(raw).__name__}, not an object of fields")
    version = raw.get("version")
    if version is None:
        raise ConfigError(f"{path} carries no 'version' key; a SIW config file states its version")
    if int(version) != VERSION:
        raise ConfigError(f"{path} declares version {version}; this SIW writes {VERSION} — "
                          "update SIW or rewrite the file (the console's Settings pane writes it)")
    values = {}
    for key, value in raw.items():
        if key == "version":
            continue
        if key not in FIELDS:
            raise ConfigError(f"{path} carries unknown field {key!r}; known: {', '.join(FIELDS)}")
        values[key] = _checked(key, value, str(path))
    return values, "file"


def _env_layer(environ: dict) -> dict:
    """The environment's fields, only for the names it actually holds (an empty value is an opinion:
    an unset string means "no corpus yet" for `SIW_CORPUS`)."""
    out = {}
    for name, field in ENV_NAMES.items():
        if name not in environ:
            continue
        raw = environ[name]
        # One parse rule for every writer, so `SIW_SELECTED=a,b`, the Settings pane, and
        # the config file cannot disagree about what the same text means.
        out[field] = _checked(field, parse_value(raw, field), f"env {name}")
    return out


def resolve(explicit: str | None = None, environ: dict | None = None) -> tuple[Config, Path | None]:
    """The config this run uses, and the file it came from (None when every value is a default).

    Layers, last write wins: defaults, then the first file that exists, then the environment. A named
    file (`--config`) that is missing is a refusal, never a silent fall through to the defaults —
    falling through would run the tool on a corpus the operator did not choose."""
    environ = os.environ if environ is None else environ
    values: dict = {}
    sources = {name: "default" for name in FIELDS}
    chosen: Path | None = None
    for candidate in search_order(explicit):
        if candidate.is_file():
            loaded, layer = load(candidate)
            values.update(loaded)
            sources.update({name: layer for name in loaded})
            chosen = candidate
            break
        if explicit:
            raise ConfigError(f"--config names a file that does not exist: {candidate}")
    for name, value in _env_layer(environ).items():
        values[name] = value
        sources[name] = "env"
    config = Config(values, path=chosen, sources=sources)
    if config.path is None and explicit:
        config.path = Path(explicit).expanduser()
    return config, config.path


def save(config: Config, path: str | Path | None = None) -> Path:
    """Write the file (creating its folder), and return the path written — the caller prints it, because
    "the tool saved a config somewhere" is not an answer an operator can act on."""
    target = Path(path).expanduser() if path else (config.path or machine_config_dir() / "config.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: config.values[name] for name in FIELDS}
    target.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    config.path = target
    for name in FIELDS:
        config.sources[name] = "file"
    return target


def pidfile(config: Config, port: int | None = None) -> Path:
    """The engine's pid file: beside the config, one per port.

    Beside the config rather than in the repo or in `/var/run`, because that folder is the one SIW knows
    is user-writable on both platforms, and per-port because two engines on one box (the deployment's and
    a test's) must not overwrite each other's record — the record is what makes `siw stop` able to refuse
    a server it did not start."""
    folder = config.path.parent if config.path else machine_config_dir()
    return folder / f"siw-engine-{port or config.values['port']}.pid"


def api_pidfile(config: Config, port: int | None = None) -> Path:
    """The API server's own pid file, when it was detached from the terminal that started it."""
    folder = config.path.parent if config.path else machine_config_dir()
    return folder / f"siw-api-{port or config.values['api_port']}.pid"


def suggest_exe(corpus: str | None) -> str | None:
    """The binary beside the corpus, tried in the layouts real corpora ship in.

    The kiwix-tools tree sits either beside the ZIM directory (`<parent>/binaries/kiwix-tools/`) or inside
    it, and the NAS layout puts `binaries` one level further up. The first that exists wins; None means
    "ask the operator", which is honest. A guessed binary that does not exist is
    worse than no guess: the launch would fail at the engine, not at the config."""
    if not corpus:
        return None
    exe = "kiwix-serve.exe" if os.name == "nt" else "kiwix-serve"
    root = Path(corpus).expanduser()
    candidates = [
        root.parent / "binaries" / "kiwix-tools" / exe,
        root.parent.parent / "binaries" / "kiwix-tools" / exe,
        root.parent / "kiwix-tools" / exe,
        root / "binaries" / "kiwix-tools" / exe,
        root / exe,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def default_summary(config: Config) -> str:
    """One line naming every value and the layer that set it, so the boot state is legible
    without opening the file or reading source. Passwords print masked."""
    parts = [f"config: {config.path or '<defaults, no file read>'}"]
    for name in FIELDS:
        if name == "version":
            continue
        value = config.values[name]
        if name == "password" and value:
            # The summary goes to the terminal; the value does not. (It is stored in the
            # config file in plain text, which the README and SECURITY.md state.)
            value = "***"
        parts.append(f"{name}={value!r} ({config.sources[name]})")
    return "\n".join(parts)


def platform_note() -> str:
    """What the running platform is, as the CLI says it."""
    return f"{sys.platform} ({'Windows' if os.name == 'nt' else 'POSIX'})"
