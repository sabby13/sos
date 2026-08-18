"""Path guards and the risk policy.

This is the layer that stands between a confused (or jailbroken) model and the user's
Windows install, so it gets tested hardest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openjarvis.config import SafetyConfig
from openjarvis.errors import SafetyError
from openjarvis.risk import RiskLevel
from openjarvis.safety.paths import expand, is_within, resolve_path
from openjarvis.safety.policy import SafetyPolicy, Verdict
from openjarvis.tools.registry import default_registry

# ------------------------------------------------------------------ path guards


def test_relative_paths_resolve_against_cwd(tmp_path):
    resolved = resolve_path("notes.txt", cwd=str(tmp_path))
    assert resolved == tmp_path / "notes.txt"


def test_well_known_names_expand():
    assert expand("desktop") == Path.home() / "Desktop"
    assert expand("Downloads") == Path.home() / "Downloads"
    assert expand("my documents") == Path.home() / "Documents"


def test_missing_path_raises_when_required(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_path("nope.txt", cwd=str(tmp_path), must_exist=True)


def test_empty_path_is_rejected():
    with pytest.raises(SafetyError):
        resolve_path("   ")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system paths")
@pytest.mark.parametrize(
    "target",
    [r"C:\Windows\System32\drivers\etc\hosts", r"C:\Program Files\anything.txt", "C:\\"],
)
def test_system_paths_are_blocked_for_writes(target):
    with pytest.raises(SafetyError, match="protected system location"):
        resolve_path(target, for_write=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system paths")
def test_system_paths_are_readable():
    """Blocking reads would break perfectly reasonable questions about the machine."""
    assert resolve_path(r"C:\Windows", for_write=False).name == "Windows"


def test_allowed_roots_confine_writes(tmp_path):
    inside = tmp_path / "project" / "file.txt"
    assert resolve_path(str(inside), for_write=True, allowed_roots=[str(tmp_path)]) == inside

    with pytest.raises(SafetyError, match="outside the allowed roots"):
        resolve_path(str(tmp_path.parent / "elsewhere.txt"),
                     for_write=True, allowed_roots=[str(tmp_path)])


def test_traversal_cannot_escape_allowed_roots(tmp_path):
    """The classic attack: a relative path that climbs out of the sandbox."""
    escape = str(tmp_path / ".." / ".." / "escaped.txt")
    with pytest.raises(SafetyError, match="outside the allowed roots"):
        resolve_path(escape, for_write=True, allowed_roots=[str(tmp_path)])


def test_denied_paths_block_even_inside_allowed_roots(tmp_path):
    secret = tmp_path / "secrets"
    secret.mkdir()
    with pytest.raises(SafetyError, match="denied path"):
        resolve_path(str(secret / "keys.txt"), for_write=True,
                     allowed_roots=[str(tmp_path)], denied_paths=[str(secret)])


def test_is_within_is_not_fooled_by_prefixes(tmp_path):
    """`/data-backup` is not inside `/data`, despite the string prefix."""
    root = tmp_path / "data"
    sibling = tmp_path / "data-backup"
    root.mkdir()
    sibling.mkdir()
    assert is_within(root / "a.txt", root)
    assert not is_within(sibling / "a.txt", root)


# ---------------------------------------------------------------------- policy


def _policy(**kwargs) -> SafetyPolicy:
    return SafetyPolicy(SafetyConfig(**kwargs))


def _tool(name: str):
    return default_registry().get(name)


def test_tiered_mode_only_stops_dangerous_tools(ctx):
    policy = _policy(mode="tiered")
    assert policy.evaluate(_tool("find_files"), None, ctx).verdict is Verdict.ALLOW
    assert policy.evaluate(_tool("create_folder"), None, ctx).verdict is Verdict.ALLOW
    assert policy.evaluate(_tool("delete_item"), None, ctx).verdict is Verdict.CONFIRM


def test_strict_mode_confirms_everything_that_changes_state(ctx):
    policy = _policy(mode="strict")
    assert policy.evaluate(_tool("list_directory"), None, ctx).verdict is Verdict.ALLOW
    assert policy.evaluate(_tool("create_folder"), None, ctx).verdict is Verdict.CONFIRM
    assert policy.evaluate(_tool("delete_item"), None, ctx).verdict is Verdict.CONFIRM


def test_auto_mode_confirms_nothing(ctx):
    policy = _policy(mode="auto")
    assert policy.evaluate(_tool("delete_item"), None, ctx).verdict is Verdict.ALLOW


def test_explicit_config_overrides_the_risk_tier(ctx):
    assert _policy(auto_approve=["delete_item"]).evaluate(
        _tool("delete_item"), None, ctx
    ).verdict is Verdict.ALLOW
    assert _policy(always_confirm=["find_files"]).evaluate(
        _tool("find_files"), None, ctx
    ).verdict is Verdict.CONFIRM


def test_destructive_tools_are_classified_as_dangerous():
    """A misfiled risk tier is a silent hole in the whole safety story."""
    registry = default_registry()
    assert registry.get("delete_item").risk is RiskLevel.DANGEROUS
    for name in ("list_directory", "find_files", "read_text_file", "file_info",
                 "list_applications", "list_windows"):
        assert registry.get(name).risk is RiskLevel.SAFE, f"{name} should be read-only"
