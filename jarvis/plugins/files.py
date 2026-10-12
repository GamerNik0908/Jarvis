"""Dateien lesen, schreiben, auflisten, suchen, verschieben und (in den Papierkorb) löschen."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
from pathlib import Path
from typing import Any

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool
from jarvis.security.permissions import Level, stricter

_TEXT_LIMIT = 200_000
_LIST_LIMIT = 300


def _path(ctx: ToolContext, raw: str) -> Path:
    return ctx.app.paths.resolve(raw)


def _size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024  # type: ignore[assignment]
    return f"{num:.1f} TB"


def _read_level(args: dict[str, Any], ctx: ToolContext) -> Level:
    return ctx.app.paths.read_level(_path(ctx, args["path"]))


@tool(
    name="files_list",
    description="List files and folders in a directory (optionally filtered by a glob pattern like '*.pdf').",
    parameters=obj(
        {
            "path": {
                "type": "string",
                "description": "Folder, e.g. 'C:/Users/Nik/Documents' or '~/Downloads'",
            },
            "pattern": {"type": "string", "description": "Optional glob filter, e.g. '*.jpg'"},
        },
        ["path"],
    ),
    level=_read_level,
    category="dateien",
)
async def files_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    folder = _path(ctx, args["path"])
    if not folder.is_dir():
        return ToolResult.error(f"Ordner nicht gefunden: {folder}")
    pattern = args.get("pattern") or "*"

    def scan() -> list[str]:
        entries = []
        for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if not fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
                continue
            if entry.is_dir():
                entries.append(f"[Ordner] {entry.name}")
            else:
                try:
                    entries.append(f"{entry.name} ({_size(entry.stat().st_size)})")
                except OSError:
                    entries.append(entry.name)
            if len(entries) >= _LIST_LIMIT:
                entries.append("… (weitere Einträge ausgelassen)")
                break
        return entries

    entries = await asyncio.to_thread(scan)
    return ToolResult(f"{folder}:\n" + ("\n".join(entries) or "(leer)"))


@tool(
    name="files_read",
    description="Read a text file (txt, md, csv, json, code, log …). Returns at most max_chars characters.",
    parameters=obj(
        {
            "path": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Default 20000"},
        },
        ["path"],
    ),
    level=_read_level,
    category="dateien",
)
async def files_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _path(ctx, args["path"])
    if not path.is_file():
        return ToolResult.error(f"Datei nicht gefunden: {path}")
    limit = min(int(args.get("max_chars") or 20_000), _TEXT_LIMIT)
    data = await asyncio.to_thread(path.read_bytes)
    if b"\x00" in data[:4096]:
        return ToolResult.error(f"{path.name} ist keine Textdatei ({_size(len(data))})")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    suffix = f"\n… (gekürzt, {len(text)} Zeichen gesamt)" if len(text) > limit else ""
    # Dateiinhalte gelten als privat: danach nur noch vertrauenswürdige Modelle.
    return ToolResult(text[:limit] + suffix, sensitive=True)


def _write_level(args: dict[str, Any], ctx: ToolContext) -> Level:
    path = _path(ctx, args["path"])
    return ctx.app.paths.write_level(path, overwrite=path.exists() and not args.get("append"))


@tool(
    name="files_write",
    description=(
        "Create or overwrite a text file. Relative paths go to JARVIS' workspace folder. "
        "Set append=true to add to the end of an existing file."
    ),
    parameters=obj(
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean"},
        },
        ["path", "content"],
    ),
    level=_write_level,
    category="dateien",
    summary=lambda a: (
        f"Datei schreiben: {a.get('path')} ({len(str(a.get('content', '')))} Zeichen)"
    ),
)
async def files_write(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _path(ctx, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.get("append") else "w"

    def write() -> None:
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(str(args["content"]))

    await asyncio.to_thread(write)
    return ToolResult(f"Gespeichert: {path}", data={"path": str(path)})


def _move_level(args: dict[str, Any], ctx: ToolContext) -> Level:
    policy = ctx.app.paths
    source = _path(ctx, args["source"])
    target = _path(ctx, args["target"])
    return stricter(
        stricter(policy.delete_level(source), policy.write_level(target, overwrite=True)),
        Level.CONFIRM,
    )


@tool(
    name="files_move",
    description="Move or rename a file or folder. Also used to organize files into folders.",
    parameters=obj(
        {"source": {"type": "string"}, "target": {"type": "string"}}, ["source", "target"]
    ),
    level=_move_level,
    category="dateien",
    summary=lambda a: f"Verschieben: {a.get('source')} → {a.get('target')}",
)
async def files_move(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    source = _path(ctx, args["source"])
    target = _path(ctx, args["target"])
    if not source.exists():
        return ToolResult.error(f"Nicht gefunden: {source}")
    if target.is_dir():
        target = target / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(source), str(target))
    return ToolResult(f"Verschoben nach {target}")


def _delete_level(args: dict[str, Any], ctx: ToolContext) -> Level:
    return ctx.app.paths.delete_level(_path(ctx, args["path"]))


@tool(
    name="files_delete",
    description="Delete a file or folder by moving it to the Windows recycle bin (recoverable).",
    parameters=obj({"path": {"type": "string"}}, ["path"]),
    level=_delete_level,
    category="dateien",
    summary=lambda a: f"In den Papierkorb verschieben: {a.get('path')}",
)
async def files_delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _path(ctx, args["path"])
    if not path.exists():
        return ToolResult.error(f"Nicht gefunden: {path}")
    try:
        from send2trash import send2trash
    except ImportError:
        return ToolResult.error("Papierkorb-Unterstützung fehlt (Paket send2trash)")
    await asyncio.to_thread(send2trash, str(path))
    return ToolResult(f"In den Papierkorb verschoben: {path}")


@tool(
    name="files_search",
    description="Search for files by name (case-insensitive substring or glob) below a folder.",
    parameters=obj(
        {
            "query": {
                "type": "string",
                "description": "Part of the file name or glob like '*.docx'",
            },
            "root": {"type": "string", "description": "Folder to search in, default: user profile"},
            "max_results": {"type": "integer"},
        },
        ["query"],
    ),
    category="dateien",
    timeout_s=90,
)
async def files_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    root = _path(ctx, args.get("root") or str(Path.home()))
    query = str(args["query"]).lower()
    limit = min(int(args.get("max_results") or 30), 200)
    skip = {"appdata", "node_modules", ".git", "$recycle.bin", "windows", ".venv", "__pycache__"}

    def walk() -> list[str]:
        found: list[str] = []
        for current, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in skip and not d.startswith(".")]
            for name in names:
                lowered = name.lower()
                if (
                    fnmatch.fnmatch(lowered, query)
                    if any(c in query for c in "*?")
                    else query in lowered
                ):
                    found.append(str(Path(current) / name))
                    if len(found) >= limit:
                        return found
        return found

    found = await asyncio.to_thread(walk)
    return ToolResult(
        "\n".join(found) if found else f"Keine Treffer für '{args['query']}' in {root}"
    )
