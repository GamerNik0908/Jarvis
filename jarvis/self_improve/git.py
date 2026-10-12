"""Git-Hilfsfunktionen für die Selbstverbesserung (Worktrees, Commits, Merges)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

GIT_IDENTITY = ("-c", "user.name=JARVIS", "-c", "user.email=jarvis@localhost")


class GitError(RuntimeError):
    pass


def git_executable() -> str | None:
    return shutil.which("git")


async def run_git(*args: str, cwd: Path, check: bool = True, limit_s: float = 120) -> str:
    exe = git_executable()
    if exe is None:
        raise GitError("Git ist nicht installiert (winget install Git.Git)")
    proc = await asyncio.create_subprocess_exec(
        exe,
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), limit_s)
    except TimeoutError:
        proc.kill()
        raise GitError(f"git {' '.join(args)}: Zeitüberschreitung") from None
    text = out.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} fehlgeschlagen: {err.decode('utf-8', 'replace').strip()[:500]}"
        )
    return text


async def is_repo(path: Path) -> bool:
    try:
        out = await run_git("rev-parse", "--is-inside-work-tree", cwd=path, check=False)
    except GitError:
        return False
    return out.strip() == "true"


async def head(path: Path) -> str:
    return (await run_git("rev-parse", "HEAD", cwd=path)).strip()


async def current_branch(path: Path) -> str:
    return (await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)).strip()


async def is_clean(path: Path) -> bool:
    """Keine Änderungen an versionierten Dateien (unversionierte Dateien sind egal)."""
    out = await run_git("status", "--porcelain", "--untracked-files=no", cwd=path)
    return not out.strip()


async def worktree_add(repo: Path, target: Path, branch: str, base: str) -> None:
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    if await asyncio.to_thread(target.exists):
        await worktree_remove(repo, target)
    await run_git("worktree", "add", "-b", branch, str(target), base, cwd=repo)


async def worktree_remove(repo: Path, target: Path) -> None:
    await run_git("worktree", "remove", "--force", str(target), cwd=repo, check=False)
    if await asyncio.to_thread(target.exists):
        await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
    await run_git("worktree", "prune", cwd=repo, check=False)


async def commit_all(path: Path, message: str) -> bool:
    """Committet alle Änderungen. ``False``, wenn es nichts zu committen gab."""
    await run_git("add", "-A", cwd=path)
    status = await run_git("status", "--porcelain", cwd=path)
    if not status.strip():
        return False
    await run_git(*GIT_IDENTITY, "commit", "-q", "-m", message, cwd=path)
    return True


async def commit_paths(path: Path, message: str, paths: Sequence[str]) -> bool:
    """Committet nur die angegebenen Dateien – eigene, unversionierte Dateien bleiben außen vor."""
    await run_git("add", "--", *paths, cwd=path)
    staged = await run_git("diff", "--cached", "--name-only", cwd=path)
    if not staged.strip():
        return False
    await run_git(*GIT_IDENTITY, "commit", "-q", "-m", message, "--", *paths, cwd=path)
    return True


async def changed_files(path: Path, base: str, ref: str = "HEAD") -> list[str]:
    out = await run_git("diff", "--name-only", f"{base}..{ref}", cwd=path)
    return [line.strip() for line in out.splitlines() if line.strip()]


async def diff_stat(path: Path, base: str, ref: str = "HEAD") -> str:
    return (await run_git("diff", "--stat", f"{base}..{ref}", cwd=path)).strip()


async def diff_text(path: Path, base: str, ref: str = "HEAD", limit: int = 400_000) -> str:
    text = await run_git("diff", f"{base}..{ref}", cwd=path, limit_s=60)
    return text if len(text) <= limit else text[:limit] + "\n… (Diff gekürzt)"


async def show_file(path: Path, ref: str, file: str) -> str | None:
    try:
        return await run_git("show", f"{ref}:{file}", cwd=path)
    except GitError:
        return None


async def merge_no_ff(repo: Path, branch: str, message: str) -> None:
    try:
        await run_git(*GIT_IDENTITY, "merge", "--no-ff", "-m", message, branch, cwd=repo)
    except GitError:
        await run_git("merge", "--abort", cwd=repo, check=False)
        raise


async def reset_hard(repo: Path, commit: str) -> None:
    await run_git("reset", "--hard", commit, cwd=repo)


async def delete_branch(repo: Path, branch: str) -> None:
    await run_git("branch", "-D", branch, cwd=repo, check=False)
