"""The tools themselves, exercised against a real temp directory and a fake OS."""

from __future__ import annotations

import time

import pytest

from openjarvis.errors import SafetyError, ToolError
from openjarvis.tools.apps import LaunchApplication, resolve_app
from openjarvis.tools.browser import OpenUrl, WebSearch, normalise_url
from openjarvis.tools.filesystem import (
    CopyItem,
    CreateFolder,
    DeleteItem,
    ListDirectory,
    MoveItem,
    ReadTextFile,
    WriteTextFile,
)
from openjarvis.tools.search import FindFiles
from openjarvis.tools.windows_ctl import ControlWindow, ListWindows


def run(tool, ctx, **kwargs):
    """Parse arguments the way the agent loop does, then execute."""
    return tool.run(tool.parse_args(kwargs), ctx)


# ------------------------------------------------------------------ filesystem


def test_list_directory_separates_files_and_folders(ctx, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("hi")
    result = run(ListDirectory(), ctx, path=str(tmp_path))
    kinds = {e["name"]: e["type"] for e in result.data["entries"]}
    assert kinds == {"sub": "folder", "a.txt": "file"}


def test_list_directory_hides_dotfiles_by_default(ctx, tmp_path):
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "visible.txt").write_text("x")
    names = {e["name"] for e in run(ListDirectory(), ctx, path=str(tmp_path)).data["entries"]}
    assert names == {"visible.txt"}
    shown = run(ListDirectory(), ctx, path=str(tmp_path), show_hidden=True).data["entries"]
    assert {e["name"] for e in shown} == {".hidden", "visible.txt"}


def test_create_folder_is_idempotent(ctx, tmp_path):
    first = run(CreateFolder(), ctx, path=str(tmp_path / "Projects"))
    assert first.data["created"] is True
    second = run(CreateFolder(), ctx, path=str(tmp_path / "Projects"))
    assert second.ok and second.data["created"] is False


def test_create_folder_refuses_to_leave_the_sandbox(ctx, tmp_path):
    with pytest.raises(SafetyError):
        run(CreateFolder(), ctx, path=str(tmp_path.parent / "escaped"))


def test_write_then_read_round_trips(ctx, tmp_path):
    target = tmp_path / "notes.md"
    run(WriteTextFile(), ctx, path=str(target), content="# Notes\nline two\n")
    result = run(ReadTextFile(), ctx, path=str(target))
    assert result.data["content"].splitlines() == ["# Notes", "line two"]


def test_write_can_append(ctx, tmp_path):
    target = tmp_path / "log.txt"
    run(WriteTextFile(), ctx, path=str(target), content="one\n")
    run(WriteTextFile(), ctx, path=str(target), content="two\n", append=True)
    assert target.read_text() == "one\ntwo\n"


def test_read_refuses_oversized_files(ctx, tmp_path):
    big = tmp_path / "big.log"
    big.write_text("x" * 100_000)
    with pytest.raises(ToolError, match="too large"):
        run(ReadTextFile(), ctx, path=str(big))


def test_bare_destination_renames_in_place(ctx, tmp_path):
    source = tmp_path / "old.txt"
    source.write_text("data")
    result = run(MoveItem(), ctx, source=str(source), destination="new.txt")
    assert (tmp_path / "new.txt").read_text() == "data"
    assert not source.exists()
    assert "Renamed" in result.summary


def test_bare_destination_matching_a_folder_moves_into_it(ctx, tmp_path):
    """'move report.pdf to Archive' means into Archive, not rename to 'Archive'."""
    (tmp_path / "Archive").mkdir()
    source = tmp_path / "report.pdf"
    source.write_text("pdf")
    run(MoveItem(), ctx, source=str(source), destination="Archive")
    assert (tmp_path / "Archive" / "report.pdf").exists()


def test_move_refuses_to_clobber_without_permission(ctx, tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    with pytest.raises(ToolError, match="already exists"):
        run(MoveItem(), ctx, source=str(tmp_path / "a.txt"), destination="b.txt")

    run(MoveItem(), ctx, source=str(tmp_path / "a.txt"), destination="b.txt", overwrite=True)
    assert (tmp_path / "b.txt").read_text() == "a"


def test_copy_handles_whole_folders(ctx, tmp_path):
    source = tmp_path / "src"
    (source / "deep").mkdir(parents=True)
    (source / "deep" / "file.txt").write_text("content")
    run(CopyItem(), ctx, source=str(source), destination=str(tmp_path / "dst"))
    assert (tmp_path / "dst" / "deep" / "file.txt").read_text() == "content"
    assert source.exists(), "copy must not remove the source"


def test_delete_tries_the_recycle_bin_first(ctx, tmp_path, platform):
    victim = tmp_path / "gone.txt"
    victim.write_text("bye")
    result = run(DeleteItem(), ctx, path=str(victim))
    assert not victim.exists()
    # The fake platform reports the recycle bin as unavailable, so we fell back.
    assert platform.trashed == [str(victim)]
    assert result.data["recycled"] is False


def test_permanent_delete_skips_the_recycle_bin(ctx, tmp_path, platform):
    victim = tmp_path / "gone.txt"
    victim.write_text("bye")
    run(DeleteItem(), ctx, path=str(victim), permanent=True)
    assert platform.trashed == []


def test_delete_preview_warns_about_folder_contents(ctx, tmp_path):
    folder = tmp_path / "stuff"
    folder.mkdir()
    (folder / "a").write_text("a")
    (folder / "b").write_text("b")
    tool = DeleteItem()
    preview = tool.preview(tool.parse_args({"path": str(folder)}), ctx)
    assert "2 items" in preview and "Recycle Bin" in preview


def test_permanent_delete_preview_says_no_undo(ctx, tmp_path):
    victim = tmp_path / "x.txt"
    victim.write_text("x")
    tool = DeleteItem()
    preview = tool.preview(tool.parse_args({"path": str(victim), "permanent": True}), ctx)
    assert "PERMANENTLY" in preview


# ---------------------------------------------------------------------- search


def test_find_files_filters_by_extension(ctx, tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.pdf").write_text("x")

    result = run(FindFiles(), ctx, extensions=["pdf"], folder=str(tmp_path))
    names = {r["name"] for r in result.data["results"]}
    assert names == {"a.pdf", "c.pdf"}


def test_find_files_sorts_newest_first(ctx, tmp_path):
    old = tmp_path / "old.pdf"
    new = tmp_path / "new.pdf"
    old.write_text("x")
    time.sleep(0.02)
    new.write_text("x")

    result = run(FindFiles(), ctx, extensions=["pdf"], folder=str(tmp_path), newest_first=True)
    assert result.data["results"][0]["name"] == "new.pdf"


def test_find_files_supports_wildcards(ctx, tmp_path):
    (tmp_path / "report-2024.xlsx").write_text("x")
    (tmp_path / "budget.xlsx").write_text("x")
    result = run(FindFiles(), ctx, query="report*", folder=str(tmp_path))
    assert {r["name"] for r in result.data["results"]} == {"report-2024.xlsx"}


def test_find_files_skips_excluded_directories(ctx, tmp_path):
    junk = tmp_path / "node_modules"
    junk.mkdir()
    (junk / "buried.py").write_text("x")
    (tmp_path / "main.py").write_text("x")
    result = run(FindFiles(), ctx, extensions=["py"], folder=str(tmp_path))
    assert {r["name"] for r in result.data["results"]} == {"main.py"}


def test_find_files_refuses_an_unbounded_search(ctx):
    with pytest.raises(ToolError, match="no filters"):
        run(FindFiles(), ctx)


# ------------------------------------------------------------------------ apps


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("VS Code", "Visual Studio Code"),
        ("vscode", "Visual Studio Code"),
        ("visual studio code", "Visual Studio Code"),
        ("Chrome", "Google Chrome"),
        ("google chrome", "Google Chrome"),
        ("notepad", "Notepad"),
    ],
)
def test_app_names_resolve_the_way_people_say_them(ctx, spoken, expected):
    app = resolve_app(spoken, ctx)
    assert app is not None, f"{spoken!r} did not resolve"
    # Alias hits keep the spoken name; installed-app hits use the real name.
    assert app.name in (expected, spoken)


def test_transcription_slips_still_resolve(ctx):
    """Whisper mishears app names constantly; fuzzy matching is the safety net."""
    assert resolve_app("visual studio kode", ctx) is not None


def test_user_aliases_win_over_everything(ctx):
    ctx.settings.tools.app_aliases = {"music": "spotify.exe"}
    app = resolve_app("music", ctx)
    assert app.target == "spotify.exe"


def test_unknown_app_suggests_alternatives(ctx):
    with pytest.raises(ToolError, match="No application named"):
        run(LaunchApplication(), ctx, app="Adobe Photoshop")


def test_launch_passes_the_project_folder_through(ctx, tmp_path, platform):
    project = tmp_path / "my-project"
    project.mkdir()
    run(LaunchApplication(), ctx, app="VS Code", open_path=str(project))
    target, args = platform.launched[0]
    assert "code" in target.lower()
    assert args == [str(project)]


# --------------------------------------------------------------------- windows


def test_list_windows_can_be_filtered(ctx):
    result = run(ListWindows(), ctx, filter="chrome")
    assert len(result.data["windows"]) == 1


def test_control_window_matches_on_process_name(ctx, platform):
    run(ControlWindow(), ctx, title="notepad", action="focus")
    assert platform.focused == [3]


def test_control_window_prefers_the_shortest_title(ctx, platform):
    """Between a main window and a dialog, the short title is the one people mean."""
    run(ControlWindow(), ctx, title="Code", action="focus")
    assert platform.focused == [1]


def test_control_window_reports_what_is_open_when_nothing_matches(ctx):
    with pytest.raises(ToolError, match="Notepad"):
        run(ControlWindow(), ctx, title="Photoshop", action="focus")


def test_closing_a_window_removes_it(ctx, platform):
    run(ControlWindow(), ctx, title="Untitled - Notepad", action="close")
    assert all(w.handle != 3 for w in platform.list_windows())


# --------------------------------------------------------------------- browser


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("github.com", "https://github.com"),
        ("https://example.com/path", "https://example.com/path"),
        ("http://localhost:3000", "http://localhost:3000"),
    ],
)
def test_urls_get_a_scheme_when_a_person_omits_one(raw, expected):
    assert normalise_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["file:///C:/Windows/System32/config", "javascript:alert(1)", "data:text/html,<h1>x"]
)
def test_dangerous_url_schemes_are_refused(raw):
    with pytest.raises(ToolError):
        normalise_url(raw)


def test_prose_is_rejected_as_a_url():
    with pytest.raises(ToolError, match="not a URL"):
        normalise_url("what is the weather today")


def test_open_url_reaches_the_browser(ctx, platform):
    run(OpenUrl(), ctx, url="github.com")
    assert platform.opened_urls == ["https://github.com"]


def test_web_search_encodes_the_query(ctx, platform):
    run(WebSearch(), ctx, query="python type hints")
    assert "python+type+hints" in platform.opened_urls[0]
