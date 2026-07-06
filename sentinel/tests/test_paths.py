"""Tests for SENTINEL_HOME path resolution in paths.py.

No pytest dependency — run directly:
    source sentinel/.venv/bin/activate
    python3 -m sentinel.tests.test_paths
"""

import os
import tempfile
from pathlib import Path

from sentinel import paths


def _with_env(value, fn):
    """Run fn with SENTINEL_HOME set/cleared, restoring the original after."""
    original = os.environ.get("SENTINEL_HOME")
    try:
        if value is None:
            os.environ.pop("SENTINEL_HOME", None)
        else:
            os.environ["SENTINEL_HOME"] = value
        return fn()
    finally:
        if original is None:
            os.environ.pop("SENTINEL_HOME", None)
        else:
            os.environ["SENTINEL_HOME"] = original


def _with_package_dir(pkg_dir, fn):
    """Run fn with paths._PACKAGE_DIR patched, restoring the original after."""
    original = paths._PACKAGE_DIR
    try:
        paths._PACKAGE_DIR = Path(pkg_dir)
        return fn()
    finally:
        paths._PACKAGE_DIR = original


def test_env_override_wins():
    home = _with_env("/tmp/custom-sentinel", paths.sentinel_home)
    assert home == Path("/tmp/custom-sentinel"), home


def test_env_override_expands_user():
    home = _with_env("~/my-sentinel", paths.sentinel_home)
    assert home == Path.home() / "my-sentinel", home


def test_legacy_package_dir_with_stores_json():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "stores.json").write_text("{}")
        home = _with_env(None, lambda: _with_package_dir(tmp, paths.sentinel_home))
        assert home == Path(tmp), home


def test_legacy_package_dir_with_data_dir():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "data").mkdir()
        home = _with_env(None, lambda: _with_package_dir(tmp, paths.sentinel_home))
        assert home == Path(tmp), home


def test_default_home_when_no_legacy_markers():
    with tempfile.TemporaryDirectory() as tmp:
        home = _with_env(None, lambda: _with_package_dir(tmp, paths.sentinel_home))
        assert home == Path.home() / "Sentinel", home


def test_derived_paths_follow_home():
    def check():
        assert paths.stores_path() == Path("/tmp/s-home/stores.json")
        assert paths.env_path() == Path("/tmp/s-home/.env")
        assert paths.data_dir() == Path("/tmp/s-home/data")
        assert paths.db_path_for() == Path("/tmp/s-home/data/sentinel.db")
        assert paths.db_path_for("acme") == Path("/tmp/s-home/data/acme.db")
        assert paths.reports_dir() == Path("/tmp/s-home/reports")
        assert paths.reports_dir("acme") == Path("/tmp/s-home/reports/acme")
    _with_env("/tmp/s-home", check)


def test_config_db_path_respects_env_home():
    def check():
        from sentinel.config import SentinelConfig
        return SentinelConfig().db_path
    db = _with_env("/tmp/s-home2", check)
    assert db == "/tmp/s-home2/data/sentinel.db", db


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run() else 0)
