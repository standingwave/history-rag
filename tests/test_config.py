"""Config precedence (env > file > default), path lists, [sources].enabled,
and failure modes. Config freezes at import, so these tests reload it; the
fixtures restore original state afterwards."""
import importlib
import pytest

@pytest.fixture
def restore_modules():
    """Reload config (and index, which derives SOURCES from it) after the
    test, with the original environment back in place. Listed FIRST in test
    args so its teardown runs LAST, after monkeypatch has restored env."""
    yield
    import config, index
    importlib.reload(config)
    importlib.reload(index)

@pytest.fixture
def reload_config(tmp_path, monkeypatch):
    import config
    def do(toml_text=None, **env):
        if toml_text is not None:
            p = tmp_path / "cfg.toml"
            p.write_text(toml_text)
            env.setdefault("CLAUDE_RAG_CONFIG", str(p))
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))
        return importlib.reload(config)
    return do

def test_precedence_env_over_file_over_default(restore_modules, reload_config):
    cfg = reload_config('[core]\ndb = "/from/file.db"\n', CLAUDE_RAG_DB=None)
    assert cfg.DB_PATH == "/from/file.db"                       # file beats default
    cfg = reload_config('[core]\ndb = "/from/file.db"\n',
                        CLAUDE_RAG_DB="/from/env.db")
    assert cfg.DB_PATH == "/from/env.db"                        # env beats file

def test_get_paths_env_string_vs_file_list(restore_modules, reload_config):
    cfg = reload_config('[git]\nroots = ["~/a", "~/b"]\n')
    assert len(cfg.get_paths("git", "roots", "CLAUDE_RAG_GIT_ROOTS")) == 2
    cfg = reload_config('[git]\nroots = ["~/a"]\n',
                        CLAUDE_RAG_GIT_ROOTS="/x:/y:/z")
    assert cfg.get_paths("git", "roots", "CLAUDE_RAG_GIT_ROOTS") == ["/x", "/y", "/z"]

def test_malformed_toml_fails_loud(restore_modules, reload_config):
    with pytest.raises(SystemExit) as e:
        reload_config("core]broken")
    assert "config error" in str(e.value)

def test_unknown_keys_warn(restore_modules, reload_config, capsys):
    reload_config("[mystery]\nx = 1\n[shell]\nbadkey = true\n")
    err = capsys.readouterr().err
    assert "unknown section [mystery]" in err
    assert "unknown key shell.badkey" in err

def test_sources_enabled_filters_and_rejects_unknown(restore_modules, reload_config):
    import index
    reload_config('[sources]\nenabled = ["shell", "git"]\n')
    importlib.reload(index)
    assert [index.source_name(s) for s in index.SOURCES] == ["shell", "git"]

    reload_config('[sources]\nenabled = ["bogus"]\n')
    with pytest.raises(SystemExit) as e:
        importlib.reload(index)
    assert "bogus" in str(e.value)
