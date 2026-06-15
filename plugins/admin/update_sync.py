"""Selective Update / Sync from the upstream public repo — admin-only.

Lets a fork (a pruned *edition* of the public repo — see
docs/claude/production-editions.md) pull changes from the public "upstream"
repo, but **only for the folders the operator selects**. The folders a fork
cares about are usually a subset of the full tree, and some may be locally
customized, so a blanket ``git pull`` is wrong. This page instead:

  1. Adds the public repo as an ``upstream`` git remote and fetches it.
  2. Auto-detects which top-level folders/paths exist locally (the default
     tracked set), and lets the admin tick/untick + save the selection.
  3. Shows a per-folder diff of what upstream changed.
  4. Applies the selected folders with a real 3-way merge (``git merge-file``
     against the merge base), so local customizations are preserved where they
     don't collide. Clean merges apply automatically; collisions are left in
     the working tree with conflict markers for the **web agent** (or the
     admin) to resolve via the existing ``git_control`` ability
     (``resolve_conflict`` / ``commit_and_push``).

Deployment models ("auto-detect"):
  • **git** — the deployment is a git repo. Full diff + 3-way merge.
  • **non-git** — the deployment is a plain copy (e.g. from
    ``scripts/build_edition.py``, which omits ``.git``). The page offers a
    one-click *Initialize git + add upstream* bootstrap (POST /init) which
    turns it into the git model so real merges become available.

Self-contained drop-in: delete this file and remove its import from
``app/main.py`` to remove the feature. Reuses the git/admin helpers from
``app.api.github`` so auth + admin-gating behave identically to the existing
source-control page.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

# Reuse the source-control helpers so token auth + admin gating never drift.
from app.api.github import (
    _PROJECT_ROOT,
    _cache_token,
    _get_token,
    _require_admin,
    _run_git,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/update", tags=["admin"])

UPSTREAM_REMOTE = "upstream"
CONFIG_FILE = _PROJECT_ROOT / "data" / "config" / "update_sync.json"
BACKUP_ROOT = _PROJECT_ROOT / ".update-backups"

# Top-level dirs are always candidates; these two are also expanded one level
# deeper so an operator can track e.g. just ``plugins/abilities`` or
# ``app/agent`` rather than the whole ``plugins`` / ``app`` tree.
_DEEP_PARENTS = ("app", "plugins")

_DEFAULT_CONFIG = {
    "upstream_url": "",
    "upstream_branch": "main",
    "tracked_paths": [],          # [] = "not configured yet" → auto-detect on first load
    "last_synced_commit": "",     # upstream tip last applied (for info)
}


# ── Config persistence ──────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.is_file():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {})
    except Exception as e:
        logger.warning("update_sync: could not read config: %s", e)
    return cfg


def _save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Git helpers (built on app.api.github._run_git) ──────────────────────────

def _is_git_repo() -> bool:
    out, _, rc = _run_git(["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


def _upstream_ref(cfg: dict) -> str:
    return f"{UPSTREAM_REMOTE}/{cfg.get('upstream_branch') or 'main'}"


def _ensure_upstream_remote(url: str) -> None:
    """Add or update the ``upstream`` remote to ``url`` (idempotent)."""
    out, _, rc = _run_git(["remote", "get-url", UPSTREAM_REMOTE])
    if rc == 0:
        if out.strip() != url:
            _run_git(["remote", "set-url", UPSTREAM_REMOTE, url])
    else:
        _run_git(["remote", "add", UPSTREAM_REMOTE, url])


def _list_tree_dirs(ref: str, prefix: str = "") -> set[str]:
    """Directory pathnames one level under ``prefix`` for a git ref."""
    args = ["ls-tree", "-d", "--name-only", ref]
    if prefix:
        args.append(prefix.rstrip("/") + "/")
    out, _, rc = _run_git(args)
    if rc != 0:
        return set()
    return {line.strip().rstrip("/") for line in out.splitlines() if line.strip()}


def _candidate_paths(cfg: dict) -> list[str]:
    """Union of syncable folders from local HEAD and upstream: top-level dirs
    plus one level under app/ and plugins/."""
    ref = _upstream_ref(cfg)
    paths: set[str] = set()
    for src in ("HEAD", ref):
        paths |= _list_tree_dirs(src)
        for parent in _DEEP_PARENTS:
            paths |= _list_tree_dirs(src, parent)
    # Drop the bare deep-parents in favor of their children if the children
    # exist, but keep the parent too so "track everything under app/" stays an
    # option. Sort with shallow paths first for a stable UI.
    return sorted(paths, key=lambda p: (p.count("/"), p))


def _numstat(cfg: dict) -> list[tuple[int, int, str]]:
    """(added, deleted, path) for every file changed between HEAD and upstream.
    ``-`` counts (binary) are reported as -1."""
    ref = _upstream_ref(cfg)
    out, _, rc = _run_git(["diff", "--numstat", "HEAD", ref], timeout=30)
    rows: list[tuple[int, int, str]] = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a = -1 if parts[0] == "-" else int(parts[0] or 0)
        d = -1 if parts[1] == "-" else int(parts[1] or 0)
        rows.append((a, d, parts[2]))
    return rows


def _path_matches(file_path: str, tracked: str) -> bool:
    return file_path == tracked or file_path.startswith(tracked.rstrip("/") + "/")


def _show_blob(ref_path: str) -> Optional[bytes]:
    """Bytes of ``ref:path`` (e.g. ``upstream/main:app/foo.py``) or None if absent."""
    env_path = ref_path
    try:
        proc = subprocess.run(
            ["git", "show", env_path],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            timeout=20,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


def _is_binary(data: Optional[bytes]) -> bool:
    if not data:
        return False
    return b"\x00" in data[:8000]


# ── Request models ──────────────────────────────────────────────────────────

class ConfigIn(BaseModel):
    upstream_url: Optional[str] = None
    upstream_branch: Optional[str] = None
    tracked_paths: Optional[list[str]] = None


class InitIn(BaseModel):
    upstream_url: str
    upstream_branch: str = "main"


class ApplyIn(BaseModel):
    paths: list[str]                 # subset of tracked paths to apply
    create_backup: bool = True


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/status")
async def status(request: Request):
    """Report deployment model, upstream config, and (in git mode) the set of
    syncable folders with whether each is present locally / tracked / changed.
    """
    _require_admin(request)
    _cache_token(_get_token())

    cfg = _load_config()
    is_git = _is_git_repo()
    if not is_git:
        return {
            "mode": "non-git",
            "is_git": False,
            "upstream_url": cfg.get("upstream_url", ""),
            "upstream_branch": cfg.get("upstream_branch", "main"),
            "message": ("This deployment is not a git repository, so changes "
                        "cannot be merged from upstream yet. Initialize git + "
                        "add the upstream remote to enable selective updates."),
        }

    upstream_url = cfg.get("upstream_url", "")
    has_upstream = False
    if upstream_url:
        _ensure_upstream_remote(upstream_url)
        out, _, rc = _run_git(["remote", "get-url", UPSTREAM_REMOTE])
        has_upstream = rc == 0 and bool(out.strip())

    candidates = _candidate_paths(cfg) if has_upstream else []
    local_dirs = set()
    for p in candidates:
        if (_PROJECT_ROOT / p).is_dir():
            local_dirs.add(p)

    # Auto-detect default tracked set on first use: every candidate that exists
    # locally. Persist it so the next load is stable.
    tracked = cfg.get("tracked_paths") or []
    if not tracked and candidates:
        tracked = sorted(local_dirs)
        cfg["tracked_paths"] = tracked
        _save_config(cfg)

    # Per-folder change counts (only meaningful once upstream is fetched).
    changed_by_path: dict[str, int] = {}
    if has_upstream:
        for _a, _d, fp in _numstat(cfg):
            # attribute each changed file to the deepest matching candidate
            best = ""
            for c in candidates:
                if _path_matches(fp, c) and len(c) > len(best):
                    best = c
            if best:
                changed_by_path[best] = changed_by_path.get(best, 0) + 1

    folders = [{
        "path": p,
        "present_locally": p in local_dirs,
        "tracked": p in tracked,
        "changed_files": changed_by_path.get(p, 0),
    } for p in candidates]

    return {
        "mode": "git",
        "is_git": True,
        "upstream_url": upstream_url,
        "upstream_branch": cfg.get("upstream_branch", "main"),
        "has_upstream": has_upstream,
        "last_synced_commit": cfg.get("last_synced_commit", ""),
        "folders": folders,
    }


@router.get("/config")
async def get_config(request: Request):
    _require_admin(request)
    return _load_config()


@router.post("/config")
async def set_config(body: ConfigIn, request: Request):
    _require_admin(request)
    cfg = _load_config()
    if body.upstream_url is not None:
        cfg["upstream_url"] = body.upstream_url.strip()
    if body.upstream_branch is not None:
        cfg["upstream_branch"] = body.upstream_branch.strip() or "main"
    if body.tracked_paths is not None:
        cfg["tracked_paths"] = sorted(set(body.tracked_paths))
    _save_config(cfg)
    if cfg.get("upstream_url") and _is_git_repo():
        _ensure_upstream_remote(cfg["upstream_url"])
    return cfg


@router.post("/init")
async def init_git(body: InitIn, request: Request):
    """Bootstrap a non-git deployment: ``git init`` + add upstream + fetch.

    Selective ``checkout``/``merge-file`` against ``upstream/<branch>`` works
    even without shared history, so this is enough to enable updates.
    """
    _require_admin(request)
    _cache_token(_get_token())

    if not _is_git_repo():
        _out, err, rc = _run_git(["init"], timeout=30)
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"git init failed: {err.strip()}")
        # Make an initial commit so HEAD exists for diffs/merge-base.
        _run_git(["add", "-A"], timeout=120)
        _run_git(["-c", "user.email=admin@webagent.local",
                  "-c", "user.name=webagent",
                  "commit", "-m", "Baseline before upstream sync", "--quiet"],
                 timeout=120)

    _ensure_upstream_remote(body.upstream_url.strip())
    branch = body.upstream_branch.strip() or "main"
    _out, err, rc = _run_git(["fetch", UPSTREAM_REMOTE, branch, "--quiet"], timeout=120)
    if rc != 0:
        detail = err.strip() or "fetch failed"
        if "Authentication failed" in err or "could not read" in err:
            detail += ("\n\nSet your GitHub token in the Source Control sidebar "
                       "if the upstream repo is private.")
        raise HTTPException(status_code=500, detail=detail)

    cfg = _load_config()
    cfg["upstream_url"] = body.upstream_url.strip()
    cfg["upstream_branch"] = branch
    _save_config(cfg)
    return {"status": "initialized", "branch": branch}


@router.post("/fetch")
async def fetch_upstream(request: Request):
    """Fetch the configured upstream branch so diffs reflect what's published."""
    _require_admin(request)
    _cache_token(_get_token())

    if not _is_git_repo():
        raise HTTPException(status_code=400, detail="Not a git repository. Initialize git first.")
    cfg = _load_config()
    url = cfg.get("upstream_url", "")
    if not url:
        raise HTTPException(status_code=400, detail="No upstream repo configured.")
    _ensure_upstream_remote(url)
    branch = cfg.get("upstream_branch") or "main"
    _out, err, rc = _run_git(["fetch", UPSTREAM_REMOTE, branch, "--quiet"], timeout=120)
    if rc != 0:
        detail = err.strip() or "fetch failed"
        if "Authentication failed" in err or "could not read" in err:
            detail += ("\n\nSet your GitHub token in the Source Control sidebar "
                       "if the upstream repo is private.")
        raise HTTPException(status_code=500, detail=detail)

    rows = _numstat(cfg)
    tip, _, _ = _run_git(["rev-parse", _upstream_ref(cfg)])
    return {
        "status": "fetched",
        "upstream_tip": tip.strip()[:7],
        "total_changed_files": len(rows),
    }


@router.get("/diff")
async def diff(request: Request, path: str = Query(..., description="Folder/path to diff vs upstream")):
    """Unified diff of one tracked path between local HEAD and upstream."""
    _require_admin(request)
    if not _is_git_repo():
        raise HTTPException(status_code=400, detail="Not a git repository.")
    cfg = _load_config()
    ref = _upstream_ref(cfg)
    out, err, rc = _run_git(["diff", "HEAD", ref, "--", path], timeout=30)
    if rc != 0 and err.strip():
        raise HTTPException(status_code=500, detail=err.strip())
    # Cap payload so a huge folder diff doesn't wedge the UI.
    return {"path": path, "diff": out[:400_000], "truncated": len(out) > 400_000}


def _merge_one_file(file_rel: str, base_ref: str, theirs_ref: str,
                    backup_dir: Optional[Path]) -> str:
    """3-way merge a single file in place. Returns a status string:
    'clean' | 'conflict' | 'added' | 'deleted-upstream' | 'binary' | 'error'.
    """
    abs_path = _PROJECT_ROOT / file_rel
    base = _show_blob(f"{base_ref}:{file_rel}") if base_ref else None
    theirs = _show_blob(f"{theirs_ref}:{file_rel}")
    ours_exists = abs_path.exists()
    ours = abs_path.read_bytes() if ours_exists else None

    # Upstream deleted the file → keep local by default; report it.
    if theirs is None:
        return "deleted-upstream"

    # Backup any local file we're about to touch.
    if backup_dir is not None and ours_exists:
        dest = backup_dir / file_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, dest)

    # New file from upstream → just write it.
    if not ours_exists:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(theirs)
        return "added"

    # Identical already.
    if ours == theirs:
        return "clean"

    # Binary: can't line-merge. Take upstream (already backed up local).
    if _is_binary(ours) or _is_binary(theirs) or _is_binary(base):
        abs_path.write_bytes(theirs)
        return "binary"

    # No merge base (e.g. init'd repo, unrelated histories): can't 3-way →
    # treat a real difference as a conflict the agent/admin must settle.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ours_tmp = tdp / "ours"
        base_tmp = tdp / "base"
        theirs_tmp = tdp / "theirs"
        ours_tmp.write_bytes(ours)
        base_tmp.write_bytes(base if base is not None else b"")
        theirs_tmp.write_bytes(theirs)
        proc = subprocess.run(
            ["git", "merge-file", "-L", "local", "-L", "base", "-L", "upstream",
             str(ours_tmp), str(base_tmp), str(theirs_tmp)],
            cwd=str(_PROJECT_ROOT), capture_output=True, timeout=30,
        )
        merged = ours_tmp.read_bytes()
        abs_path.write_bytes(merged)
        # merge-file: 0 = clean, >0 = number of conflicts, <0 = error.
        if proc.returncode == 0:
            return "clean"
        if proc.returncode > 0:
            return "conflict"
        return "error"


@router.post("/apply")
async def apply(body: ApplyIn, request: Request):
    """Apply upstream changes for the selected paths via 3-way merge.

    Clean merges are written directly; collisions are written with conflict
    markers and returned so the web agent (or admin) can resolve them.
    """
    _require_admin(request)
    _cache_token(_get_token())

    if not _is_git_repo():
        raise HTTPException(status_code=400, detail="Not a git repository.")
    cfg = _load_config()
    ref = _upstream_ref(cfg)
    base_out, _, base_rc = _run_git(["merge-base", "HEAD", ref])
    base_ref = base_out.strip() if base_rc == 0 else ""

    selected = set(body.paths or [])
    if not selected:
        raise HTTPException(status_code=400, detail="No paths selected.")

    # Collect changed files that fall under any selected path.
    files = [fp for _a, _d, fp in _numstat(cfg)
             if any(_path_matches(fp, p) for p in selected)]

    backup_dir = None
    if body.create_backup and files:
        backup_dir = BACKUP_ROOT / time.strftime("%Y%m%d-%H%M%S")
        backup_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, list[str]] = {
        "clean": [], "added": [], "conflict": [],
        "binary": [], "deleted-upstream": [], "error": [],
    }
    for fp in files:
        try:
            status_str = _merge_one_file(fp, base_ref, ref, backup_dir)
        except Exception as e:
            logger.warning("update_sync: merge failed for %s: %s", fp, e)
            status_str = "error"
        result.setdefault(status_str, []).append(fp)

    backend_changed = any(
        f.endswith(".py")
        for k in ("clean", "added", "binary")
        for f in result.get(k, [])
    )

    # Record the upstream tip we just merged against (informational).
    tip, _, _ = _run_git(["rev-parse", ref])
    if tip.strip():
        cfg["last_synced_commit"] = tip.strip()
        _save_config(cfg)

    return {
        "status": "applied",
        "results": result,
        "conflict_files": result.get("conflict", []),
        "backend_changed": backend_changed,
        "backup_dir": str(backup_dir) if backup_dir else None,
    }
