"""File and folder tools.

Descriptions here are prompt engineering: they are the only thing the model reads when
deciding which tool to call, so they say what the tool does *and* when to prefer it over
a neighbour.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from ..errors import ToolError
from ..safety.paths import describe, human_size
from ._fsutil import entry_summary, resolve
from .base import RiskLevel, Tool, ToolContext, ToolResult

# Reading an arbitrary file into a prompt is the easiest way to blow a context window.
MAX_READ_BYTES = 60_000


class ListDirectoryArgs(BaseModel):
    path: str = Field(
        default=".",
        description="Folder to list. Accepts an absolute path, or a shortcut name like "
        "'desktop', 'downloads', 'documents', 'home'.",
    )
    show_hidden: bool = Field(default=False, description="Include dotfiles and hidden entries.")
    limit: int = Field(default=60, ge=1, le=300, description="Maximum entries to return.")


class ListDirectory(Tool[ListDirectoryArgs]):
    name = "list_directory"
    description = """
    List the files and folders directly inside one folder (not recursive).
    Use this when the user names a specific folder. To hunt for a file whose location
    is unknown, use find_files instead.
    """
    risk = RiskLevel.SAFE
    args_model = ListDirectoryArgs

    def run(self, args: ListDirectoryArgs, ctx: ToolContext) -> ToolResult:
        folder = resolve(ctx, args.path, must_exist=True)
        if not folder.is_dir():
            raise ToolError(f"{describe(folder)} is a file, not a folder.")

        entries = []
        for child in sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if not args.show_hidden and child.name.startswith("."):
                continue
            entries.append(entry_summary(child))
            if len(entries) >= args.limit:
                break

        ctx.remember_path(str(folder))
        folders = sum(1 for e in entries if e["type"] == "folder")
        files = len(entries) - folders
        return ToolResult.success(
            f"{describe(folder)} contains {folders} folders and {files} files.",
            {"folder": str(folder), "entries": entries},
        )


class ReadTextFileArgs(BaseModel):
    path: str = Field(description="File to read.")
    max_lines: int = Field(default=200, ge=1, le=2000, description="Stop after this many lines.")


class ReadTextFile(Tool[ReadTextFileArgs]):
    name = "read_text_file"
    description = """
    Read the contents of a text file (code, notes, config, csv, markdown).
    Only use this when the user wants the content summarised, searched, or explained.
    To simply show a file to the user, call open_file so it opens in a real application.
    """
    risk = RiskLevel.SAFE
    args_model = ReadTextFileArgs

    def run(self, args: ReadTextFileArgs, ctx: ToolContext) -> ToolResult:
        path = resolve(ctx, args.path, must_exist=True)
        if path.is_dir():
            raise ToolError(f"{describe(path)} is a folder. Use list_directory for folders.")

        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(
                f"{describe(path)} is {human_size(size)}, too large to read into context. "
                "Ask the user what specifically they need from it."
            )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Could not read {describe(path)}: {exc}") from exc

        lines = text.splitlines()
        truncated = len(lines) > args.max_lines
        body = "\n".join(lines[: args.max_lines])

        ctx.remember_path(str(path))
        return ToolResult.success(
            f"Read {len(lines)} lines from {path.name}.",
            {"path": str(path), "truncated": truncated, "content": body},
        )


class FileInfoArgs(BaseModel):
    path: str = Field(description="File or folder to inspect.")


class FileInfo(Tool[FileInfoArgs]):
    name = "file_info"
    description = """
    Get size, type, and modification time for one file or folder, and whether it exists.
    Cheap - use it to check something before renaming, moving, or deleting it.
    """
    risk = RiskLevel.SAFE
    args_model = FileInfoArgs

    def run(self, args: FileInfoArgs, ctx: ToolContext) -> ToolResult:
        path = resolve(ctx, args.path)
        if not path.exists():
            return ToolResult.success(
                f"{describe(path)} does not exist.", {"path": str(path), "exists": False}
            )
        info = entry_summary(path)
        info["exists"] = True
        if path.is_dir():
            try:
                info["item_count"] = sum(1 for _ in path.iterdir())
            except OSError:
                info["item_count"] = None
        ctx.remember_path(str(path))
        return ToolResult.success(
            f"{path.name} is a {info['type']}"
            + (f", {info['size']}" if info.get("size") else "")
            + f", last modified {info['modified']}.",
            info,
        )


class CreateFolderArgs(BaseModel):
    path: str = Field(
        description="Folder to create. A bare name like 'Projects' is created inside the "
        "current working folder; pass a full path to place it elsewhere."
    )
    parents: bool = Field(default=True, description="Create missing intermediate folders too.")


class CreateFolder(Tool[CreateFolderArgs]):
    name = "create_folder"
    description = "Create a new folder. Succeeds quietly if it already exists."
    risk = RiskLevel.CAUTION
    args_model = CreateFolderArgs

    def preview(self, args: CreateFolderArgs, ctx: ToolContext) -> str:
        return f"Create folder {args.path}"

    def run(self, args: CreateFolderArgs, ctx: ToolContext) -> ToolResult:
        path = resolve(ctx, args.path, for_write=True)
        if path.exists():
            if path.is_dir():
                return ToolResult.success(
                    f"{describe(path)} already exists.", {"path": str(path), "created": False}
                )
            raise ToolError(f"{describe(path)} already exists as a file.")
        try:
            path.mkdir(parents=args.parents, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"Could not create {describe(path)}: {exc}") from exc

        ctx.remember_path(str(path))
        return ToolResult.success(
            f"Created {describe(path)}.", {"path": str(path), "created": True}
        )


class WriteTextFileArgs(BaseModel):
    path: str = Field(description="File to write.")
    content: str = Field(description="Full text content to write.")
    append: bool = Field(default=False, description="Append instead of replacing the file.")


class WriteTextFile(Tool[WriteTextFileArgs]):
    name = "write_text_file"
    description = """
    Create a text file, or append to one. Overwriting an existing file requires the
    user to confirm, so prefer append=true when adding to notes or logs.
    """
    risk = RiskLevel.CAUTION
    args_model = WriteTextFileArgs

    def preview(self, args: WriteTextFileArgs, ctx: ToolContext) -> str:
        verb = "Append to" if args.append else "Write"
        preview = args.content[:120].replace("\n", " ")
        ellipsis = "..." if len(args.content) > 120 else ""
        return f"{verb} {args.path} ({len(args.content)} chars): {preview}{ellipsis}"

    def run(self, args: WriteTextFileArgs, ctx: ToolContext) -> ToolResult:
        path = resolve(ctx, args.path, for_write=True)
        overwriting = path.exists() and not args.append
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if args.append else "w", encoding="utf-8", newline="\n") as handle:
                handle.write(args.content)
        except OSError as exc:
            raise ToolError(f"Could not write {describe(path)}: {exc}") from exc

        ctx.remember_path(str(path))
        verb = "Appended to" if args.append else ("Overwrote" if overwriting else "Created")
        return ToolResult.success(
            f"{verb} {describe(path)}.",
            {"path": str(path), "bytes": len(args.content.encode()), "overwrote": overwriting},
        )


class MoveArgs(BaseModel):
    source: str = Field(description="Existing file or folder to move or rename.")
    destination: str = Field(
        description="New path. A bare name (no separators) renames the item in place; "
        "an existing folder moves it inside that folder."
    )
    overwrite: bool = Field(default=False, description="Replace the destination if it exists.")


class MoveItem(Tool[MoveArgs]):
    name = "move_item"
    description = """
    Rename or move a file or folder. This is the tool for 'rename this file' - pass the
    new name alone as the destination to keep it in the same folder.
    """
    risk = RiskLevel.CAUTION
    args_model = MoveArgs

    def preview(self, args: MoveArgs, ctx: ToolContext) -> str:
        action = "Overwrite and move" if args.overwrite else "Move"
        return f"{action} {args.source} -> {args.destination}"

    def run(self, args: MoveArgs, ctx: ToolContext) -> ToolResult:
        source = resolve(ctx, args.source, must_exist=True, for_write=True)
        destination = _resolve_destination(ctx, args.destination, source)

        if destination.exists():
            if not args.overwrite:
                raise ToolError(
                    f"{describe(destination)} already exists. "
                    "Ask the user whether to overwrite it, then retry with overwrite=true."
                )
            _remove(destination, ctx)

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        except OSError as exc:
            raise ToolError(f"Could not move {describe(source)}: {exc}") from exc

        ctx.remember_path(str(destination))
        renamed = source.parent == destination.parent
        verb = "Renamed" if renamed else "Moved"
        target = destination.name if renamed else describe(destination)
        return ToolResult.success(
            f"{verb} {source.name} to {target}.",
            {"source": str(source), "destination": str(destination)},
        )


class CopyArgs(BaseModel):
    source: str = Field(description="File or folder to copy.")
    destination: str = Field(description="Where to copy it to.")
    overwrite: bool = Field(default=False, description="Replace the destination if it exists.")


class CopyItem(Tool[CopyArgs]):
    name = "copy_item"
    description = "Copy a file or a whole folder to another location."
    risk = RiskLevel.CAUTION
    args_model = CopyArgs

    def preview(self, args: CopyArgs, ctx: ToolContext) -> str:
        return f"Copy {args.source} -> {args.destination}"

    def run(self, args: CopyArgs, ctx: ToolContext) -> ToolResult:
        source = resolve(ctx, args.source, must_exist=True)
        destination = _resolve_destination(ctx, args.destination, source)

        if destination.exists() and not args.overwrite:
            raise ToolError(
                f"{describe(destination)} already exists. Retry with overwrite=true to replace it."
            )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=args.overwrite)
            else:
                shutil.copy2(source, destination)
        except OSError as exc:
            raise ToolError(f"Could not copy {describe(source)}: {exc}") from exc

        ctx.remember_path(str(destination))
        return ToolResult.success(
            f"Copied {source.name} to {describe(destination)}.",
            {"source": str(source), "destination": str(destination)},
        )


class DeleteArgs(BaseModel):
    path: str = Field(description="File or folder to delete.")
    permanent: bool = Field(
        default=False,
        description="Bypass the Recycle Bin and delete irreversibly. Only set this when "
        "the user explicitly asks for a permanent delete.",
    )


class DeleteItem(Tool[DeleteArgs]):
    name = "delete_item"
    description = """
    Delete a file or folder. Goes to the Recycle Bin by default so it can be restored.
    Always confirm the exact path with the user before calling this.
    """
    risk = RiskLevel.DANGEROUS
    args_model = DeleteArgs

    def preview(self, args: DeleteArgs, ctx: ToolContext) -> str:
        try:
            path = resolve(ctx, args.path, must_exist=True)
        except (OSError, ValueError):
            return f"Delete {args.path}"
        kind = "folder" if path.is_dir() else "file"
        if path.is_dir():
            try:
                count = sum(1 for _ in path.rglob("*"))
                kind = f"folder containing {count} items"
            except OSError:
                pass
        where = "PERMANENTLY (no undo)" if args.permanent else "to the Recycle Bin"
        return f"Delete {kind} {path} {where}"

    def run(self, args: DeleteArgs, ctx: ToolContext) -> ToolResult:
        path = resolve(ctx, args.path, must_exist=True, for_write=True)
        recycled = _remove(path, ctx, permanent=args.permanent)
        where = "moved to the Recycle Bin" if recycled else "permanently deleted"
        return ToolResult.success(
            f"{path.name} was {where}.", {"path": str(path), "recycled": recycled}
        )


class OpenFileArgs(BaseModel):
    path: str = Field(description="File or folder to open.")
    reveal: bool = Field(
        default=False,
        description="Show the item highlighted in the file manager instead of opening it.",
    )


class OpenFile(Tool[OpenFileArgs]):
    name = "open_file"
    description = """
    Open a file in whichever application owns its type (PDF in the PDF reader, .py in the
    code editor), or open a folder in the file manager. Set reveal=true for
    'show me where that is' rather than 'open it'.
    """
    risk = RiskLevel.CAUTION
    args_model = OpenFileArgs

    def preview(self, args: OpenFileArgs, ctx: ToolContext) -> str:
        return f"{'Reveal' if args.reveal else 'Open'} {args.path}"

    def run(self, args: OpenFileArgs, ctx: ToolContext) -> ToolResult:
        path = resolve(ctx, args.path, must_exist=True)
        if args.reveal or path.is_dir():
            ctx.platform.reveal(path)
            action = "Opened folder" if path.is_dir() and not args.reveal else "Revealed"
        else:
            ctx.platform.open_with_default_app(path)
            action = "Opened"
        ctx.remember_path(str(path))
        return ToolResult.success(f"{action} {path.name}.", {"path": str(path)})


# --------------------------------------------------------------------- helpers


def _resolve_destination(ctx: ToolContext, raw: str, source: Path) -> Path:
    """Interpret a destination the way a person would.

    A bare name means "rename in place"; an existing folder means "put it inside".
    """
    has_separator = "/" in raw or "\\" in raw
    if not has_separator:
        candidate = source.parent / raw
        # A bare name that happens to match a sibling folder means "move into it".
        if candidate.is_dir():
            return candidate / source.name
        return resolve(ctx, str(candidate), for_write=True)

    destination = resolve(ctx, raw, for_write=True)
    if destination.is_dir():
        return destination / source.name
    return destination


def _remove(path: Path, ctx: ToolContext, *, permanent: bool = False) -> bool:
    """Delete ``path``. Returns True if it went to the Recycle Bin / Trash."""
    if not permanent and ctx.settings.safety.prefer_recycle_bin:
        try:
            if ctx.platform.move_to_trash(path):
                return True
        except OSError:
            pass  # fall through to a hard delete

    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        raise ToolError(f"Could not delete {describe(path)}: {exc}") from exc
    return False


FILESYSTEM_TOOLS = [
    ListDirectory(),
    ReadTextFile(),
    FileInfo(),
    CreateFolder(),
    WriteTextFile(),
    MoveItem(),
    CopyItem(),
    DeleteItem(),
    OpenFile(),
]
