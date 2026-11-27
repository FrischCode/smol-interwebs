"""The config's promises, tested: the order the layers win in, the closed field set, and the fact that a
saved file names itself. Each test builds its own files under `tmp_path`, so the machine's own
user-level config is never read and never written."""
import json
import os

import pytest

from siw import config


def write(tmp_path, values, name="config.json"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_the_default_endpoint_is_loopback_and_the_named_ports():
    """A default that binds every interface is a deploy that shares the machine, so loopback is not a
    documentation habit but the value a fresh config holds; and the two ports differ, because the API and
    its engine cannot both own one."""
    config_ = config.Config()
    assert config_.host == "127.0.0.1"
    assert config_.bind == "127.0.0.1"
    assert config_.port != config_.api_port


def test_an_unknown_field_is_a_named_refusal(tmp_path):
    """A typo is not an unset value: a field the loader does not know would otherwise be dropped, and the
    operator would keep editing a line the tool never reads."""
    path = write(tmp_path, {"version": 1, "corpus": "/srv/zims", "por": 18100})
    with pytest.raises(config.ConfigError) as caught:
        config.load(path)
    assert "por" in str(caught.value) and "port" in str(caught.value)


def test_a_wrong_type_is_refused_with_what_it_got(tmp_path):
    """`port: "18099"` (a string, from a hand edit) would reach a socket call and fail far from here, so
    the refusal states the field, the wanted type and what arrived."""
    path = write(tmp_path, {"version": 1, "port": "18099"})
    with pytest.raises(config.ConfigError) as caught:
        config.load(path)
    assert "port" in str(caught.value) and "int" in str(caught.value)


def test_a_file_without_a_version_is_refused(tmp_path):
    """The version key is the file's own shape stamp; a file that carries none cannot be told apart from
    one written before there were versions, so the loader asks for it rather than guessing."""
    path = write(tmp_path, {"corpus": "/tmp/zims"})
    with pytest.raises(config.ConfigError) as caught:
        config.load(path)
    assert "version" in str(caught.value)


def test_a_file_from_a_future_shape_is_refused_whole(tmp_path):
    """Half-reading a future config file is how a tool silently runs on defaults while the operator
    believes their settings: the refusal says which version the file declares and what to do."""
    path = write(tmp_path, {"version": 99, "port": 19000})
    with pytest.raises(config.ConfigError) as caught:
        config.load(path)
    assert "99" in str(caught.value)


def test_an_out_of_range_port_is_refused(tmp_path):
    """A port outside 1..65535 is a config error, not a socket error at the moment of the launch."""
    path = write(tmp_path, {"version": 1, "port": 0})
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_a_named_file_that_does_not_exist_is_refused_not_falled_through(tmp_path):
    """Falling through to the defaults when `--config` names a missing file would run the tool on a corpus
    the operator did not choose — the failure mode the whole config layer exists to prevent."""
    missing = str(tmp_path / "nope" / "siw.json")
    with pytest.raises(config.ConfigError) as caught:
        config.resolve(explicit=missing, environ={})
    assert "does not exist" in str(caught.value)


def test_the_environment_sits_above_the_file(tmp_path):
    """A shell export is an explicit act for this run; a file is a standing opinion. The export wins, and
    the source a finding prints is the environment's, not the file's."""
    path = write(tmp_path, {"version": 1, "corpus": "/srv/zims/file", "port": 18099})
    config_, _ = config.resolve(explicit=str(path), environ={"SIW_PORT": "19000"})
    assert config_.port == 19000 and config_.sources["port"] == "env"
    assert config_.corpus == "/srv/zims/file" and config_.sources["corpus"] == "file"


def test_open_browser_is_a_config_and_env_bool(tmp_path):
    """The front door's tab is a setting: true by default, false in the file, and the env layer
    parses the boolean words — 0/1 are not ints here."""
    path = write(tmp_path, {"version": 1, "open_browser": False})
    cfg, _ = config.resolve(explicit=str(path), environ={})
    assert cfg.open_browser is False and cfg.sources["open_browser"] == "file"
    cfg2, _ = config.resolve(explicit=str(path), environ={"SIW_OPEN_BROWSER": "1"})
    assert cfg2.open_browser is True and cfg2.sources["open_browser"] == "env"
    cfg3, _ = config.resolve(explicit=str(path), environ={"SIW_OPEN_BROWSER": "0"})
    assert cfg3.open_browser is False
    with pytest.raises(config.ConfigError):
        config.resolve(explicit=str(path), environ={"SIW_OPEN_BROWSER": "maybe"})


def test_the_environment_reads_a_list_field_as_a_list(tmp_path):
    """`SIW_SELECTED` is the one list field: comma-separated names, and an empty value means "no pick,
    the whole corpus" rather than "a pick of nothing"."""
    config_, _ = config.resolve(environ={"SIW_SELECTED": "a.zim, b.zim ,c.zim"})
    assert config_.selected == ["a.zim", "b.zim", "c.zim"]
    whole, _ = config.resolve(environ={"SIW_SELECTED": "   "})
    assert whole.selected is None


def test_resolve_names_the_file_it_read(tmp_path):
    """Which file did you read? The tool answers without being asked, because a comment in a file that
    names a port nobody uses is the drift this repo was written to end."""
    path = write(tmp_path, {"version": 1, "corpus": "/srv/zims"})
    config_, resolved = config.resolve(explicit=str(path), environ={})
    assert resolved == path
    assert "config.json" in config.default_summary(config_)


def test_defaults_are_reported_as_defaults(tmp_path, monkeypatch):
    """A value nobody set is reported as a default rather than dressed up as a choice — the difference
    between "SIW picked this" and "you told SIW this". The machine-config tier is pointed at an empty
    directory: a default test must not depend on whether this host happens to have a config."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("SIW_CONFIG", raising=False)
    config_, resolved = config.resolve(explicit=None, environ={})
    assert resolved is None
    assert "defaults" in config.default_summary(config_)
    assert config_.sources["port"] == "default"


def test_save_writes_the_closed_shape_and_reads_back(tmp_path):
    """What is written is what is read: every declared field, the version stamp, and a round trip that
    changes nothing. `set()` validates, so a value the loader would refuse never reaches the file."""
    target = tmp_path / "deep" / "nested" / "config.json"
    config_ = config.Config({"corpus": "/srv/zims", "selected": ["one.zim", "two.zim"]})
    config_.set("port", 19100)
    written = config.save(config_, target)
    assert written == target and target.is_file()
    again, _ = config.resolve(explicit=str(target), environ={})
    assert again.as_dict() == json.loads(target.read_text(encoding="utf-8"))
    assert again.port == 19100 and again.selected == ["one.zim", "two.zim"]
    with pytest.raises(config.ConfigError):
        config_.set("port", 70000)


def test_the_pidfile_is_per_port_and_beside_the_config(tmp_path):
    """One pidfile per port, because two engines on a box (the deployment's and a test's) must not
    overwrite each other's record — that record is the only thing `siw stop` is allowed to act on."""
    path = write(tmp_path, {"version": 1, "corpus": "/srv/zims"})
    config_, _ = config.resolve(explicit=str(path), environ={})
    first = config.pidfile(config_)
    second = config.pidfile(config_, 18101)
    assert first.parent == tmp_path and second.parent == tmp_path
    assert first != second and "18101" in second.name


def test_suggest_exe_finds_the_binary_beside_the_corpus(tmp_path):
    """The exe default is a search of the layouts corpora actually ship in, and it returns nothing rather
    than a path that does not exist: a guessed binary fails at the engine, where the cause is invisible."""
    corpus_root = tmp_path / "kiwix" / "zims"
    other = tmp_path / "kiwix" / "binaries" / "kiwix-tools"
    other.mkdir(parents=True)
    exe = other / ("kiwix-serve.exe" if os.name == "nt" else "kiwix-serve")
    exe.write_text("", encoding="utf-8")
    assert config.suggest_exe(str(corpus_root)) == str(exe)
    assert config.suggest_exe(str(tmp_path / "nowhere")) is None
    assert config.suggest_exe(None) is None


def test_launch_form_is_a_closed_set(tmp_path):
    """`launch` picks how the set reaches the engine, so an unknown spelling is refused at the config,
    not silently treated as the default at the launch."""
    with pytest.raises(config.ConfigError):
        config.Config().set("launch", "whatever")
    assert config.Config().set("launch", "library").launch == "library"


def test_parse_value_is_the_one_rule_for_cli_text_and_env_text():
    """A value written on a shell command line and a value exported into the environment arrive as strings
    and must mean the same thing. `null` and empty are nothing, digits are an int, everything else is the
    text — except the list field, where one argument cannot be a list without a separator, so `a,b` is the
    spelling and a JSON array is accepted because that is what a script writes. A malformed array is a named
    refusal here rather than a type error two layers later, which is the difference between "your list is
    unreadable" and "selected wants a list, got str"."""
    assert config.parse_value("null") is None
    assert config.parse_value("   ") is None
    assert config.parse_value("18099") == 18099
    assert config.parse_value("-1") == -1
    assert config.parse_value("C:/zims") == "C:/zims"
    assert config.parse_value("a, b ,c", "selected") == ["a", "b", "c"]
    assert config.parse_value('["a", "b"]', "selected") == ["a", "b"]
    assert config.parse_value("null", "selected") is None
    assert config.parse_value("[8]", "port") == "[8]", "a non-list field never splits"
    with pytest.raises(config.ConfigError):
        config.parse_value("[oops", "selected")
    with pytest.raises(config.ConfigError):
        config.parse_value('{"a": 1}', "selected")



def test_the_shipped_example_config_loads():
    """B8 [repro]: the one file that teaches the config format could not be loaded --
    raw `C:\\...` backslashes are not JSON escapes. It must parse through the same
    loader the real config goes through."""
    import pathlib
    example = pathlib.Path(__file__).resolve().parent.parent / "siw.example.json"
    values, layer = config.load(example)
    assert layer == "file"
    assert values["api_port"] == 7662 and values["port"] == 18099
    assert values["launch"] == "auto" and values["bind"] == "127.0.0.1"


def test_default_summary_masks_the_password():
    """m2: the summary is printed to a terminal, often into a recording or a screenshot.
    It names every value and its layer -- every value except the secret, which prints
    masked. (The config file stores it in plain text; SECURITY.md says so.)"""
    cfg = config.Config({"username": "op", "password": "hunter2"}, path=None)
    text = config.default_summary(cfg)
    assert "hunter2" not in text
    assert "'***'" in text and "username='op'" in text
