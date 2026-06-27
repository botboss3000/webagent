"""GitHub / Git integration API for webAgent.

Provides endpoints for:
- Repo status (branch, remote, unstaged/staged files)
- Commit log
- Stage all + commit
- Push to remote
- Pull from remote
- Store / check GitHub token (for HTTPS auth)

Note: the GitHub token is a single shared credential (the repo is shared).
Unlike LLM keys, it's NOT per-user — stored in provider.json only.
"""

import asyncio
import base64
import logging
import os
import re
import subprocess
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.auth.identity import request_user_id
from app import production_mirror
from app import git_repos

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github")

# ── Find project root (parent of app/) ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Token storage (single shared token in provider.json) ──
_TOKEN_FILE = _PROJECT_ROOT / "data" / "config" / "provider.json"


def _get_token() -> str:
    """Read stored GitHub token from provider.json."""
    try:
        if _TOKEN_FILE.is_file():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            return data.get("github_token", "")
    except Exception:
        pass
    return ""


def _save_token(token: str) -> None:
    """Write GitHub token to provider.json."""
    try:
        data = {}
        if _TOKEN_FILE.is_file():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        data["github_token"] = token
        _TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("Failed to save GitHub token: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to save token: {e}")


# ── Token cache (set before sync git calls) ──
_TOKEN_CACHE: str = ""


def _cache_token(token: str) -> None:
    global _TOKEN_CACHE
    _TOKEN_CACHE = token


# ── Active repo (which folder the Git page operates on) ──
# The Source Control page can point at OTHER local repos besides this app's own —
# see app/git_repos.py. `_ACTIVE_REPO` is the folder every git call below runs in;
# it is re-resolved from the saved selection at the top of each UI request via
# `_use_active_repo()` and defaults to this app's own root. The agent's Git Control
# ability pins it back here (`_pin_to_project_root`) so an agent never follows the
# page's selection. Same single-process global convention as `_TOKEN_CACHE`.
_ACTIVE_REPO: Path = _PROJECT_ROOT


def _use_active_repo() -> None:
    """Point subsequent git calls at the user's currently selected repo + key.
    Call at the top of each UI endpoint (replaces the old `_cache_token` line).
    Falls back to this app's repo + shared token on any error so the page never
    breaks if the registry is missing or a folder went away."""
    global _ACTIVE_REPO
    try:
        root, token = git_repos.resolve_active()
        _ACTIVE_REPO = root if (root and root.exists()) else _PROJECT_ROOT
        _cache_token(token or _get_token())
    except Exception:  # noqa: BLE001
        _ACTIVE_REPO = _PROJECT_ROOT
        _cache_token(_get_token())


def _pin_to_project_root() -> None:
    """Force subsequent git calls back to this app's own repo + shared key. Used by
    the agent Git Control ability so agent git is never re-pointed by whatever repo
    the user has selected on the Source Control page."""
    global _ACTIVE_REPO
    _ACTIVE_REPO = _PROJECT_ROOT
    _cache_token(_get_token())


def _run_git(args: list[str], timeout: int = 15) -> tuple[str, str, int]:
    """Run a git command in the active repo (the project root by default).
    Returns (stdout, stderr, returncode).

    Auth is injected DIRECTLY: the stored GitHub token is carried as an HTTP
    Basic-auth header (`-c http.extraHeader=…`) and the machine credential helper
    is disabled for this call (`-c credential.helper=`). This is the fix for the
    "commit & push stuck on Starting…" hang. The old code set GIT_USERNAME /
    GIT_PASSWORD, **which git does not read** — so a push fell through to whatever
    credential helper the host has (on Windows, Git Credential Manager). From this
    headless server process (no interactive desktop) that helper tries to pop a
    prompt nobody can answer, so `git push`/`pull` HANGS until the timeout and the
    UI sits forever on "Starting…". Injecting the header + clearing the helper
    means git authenticates with OUR token and, on a bad/again-missing token,
    fails INSTANTLY with a readable error instead of blocking. GIT_TERMINAL_PROMPT
    stays 0 as a second backstop against any interactive prompt.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    config_args: list[str] = []
    token = _TOKEN_CACHE or _get_token()
    if token:
        # An empty credential.helper value is git's documented way to reset the
        # helper list (so the host's Credential Manager is bypassed); the token
        # then rides as a one-shot Basic header — never persisted to .git/config.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        config_args = [
            "-c", "credential.helper=",
            "-c", f"http.extraHeader=Authorization: Basic {basic}",
        ]
    try:
        proc = subprocess.run(
            ["git"] + config_args + args,
            cwd=str(_ACTIVE_REPO),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return "", "git command timed out", -1
    except FileNotFoundError:
        return "", "git not found on this system", -1
    except Exception as e:
        return "", str(e), -1


def _git_push(timeout: int = 120) -> tuple[str, str, int]:
    """Push the current branch, self-healing a missing upstream link.

    A plain `git push` fails when the local branch has no upstream set —
    e.g. after a history rewrite / force-push drops the tracking link —
    reporting "has no upstream branch". When that's the failure we retry
    once with `--set-upstream origin <branch>`, so the push lands AND the
    link is restored for next time. This is the same effect as the git
    config `push.autoSetupRemote=true`, but applied in-code so it works
    regardless of the user's global git settings. Returns the
    (stdout, stderr, returncode) of whichever push actually ran.
    """
    out, err, code = _run_git(["push"], timeout=timeout)
    if code != 0 and "no upstream branch" in (err or "").lower():
        branch_out, _, br_code = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        branch = branch_out.strip()
        if br_code == 0 and branch and branch != "HEAD":
            out, err, code = _run_git(
                ["push", "--set-upstream", "origin", branch], timeout=timeout
            )
    return out, err, code


def _push_failure_hint(stderr: str, stdout: str) -> str:
    """Turn a raw ``git push`` failure into an actionable, plain-language message.

    The raw git error ("remote: Write access to repository not granted", "fatal:
    Authentication failed", "git command timed out", …) is accurate but doesn't
    tell a non-git user WHAT to do. We keep the raw text (nothing hidden) and
    append a specific next step based on which failure it is — so a push that
    can't succeed says so immediately and clearly instead of leaving the button
    spinning. Matched newest-cause-first so the most specific hint wins."""
    raw = (stderr or stdout or "").strip()
    low = raw.lower()
    if not (_TOKEN_CACHE or _get_token()):
        hint = ("No GitHub token is set for this repository. Add one in the "
                "source-control sidebar, then try again.")
    elif "timed out" in low:
        hint = ("Couldn't reach GitHub in time. Check your connection and that "
                "the GitHub token in the source-control sidebar is still valid.")
    elif ("write access" in low or "not granted" in low or "403" in low
          or ("permission" in low and "denied" in low)):
        hint = ("Your saved GitHub token doesn't have WRITE access to this "
                "repository. On GitHub, grant the token 'Contents: Read and "
                "write' for this repo (or paste a token that has it in the "
                "source-control sidebar).")
    elif ("authentication failed" in low or "could not read" in low
          or "invalid username or password" in low or "401" in low):
        hint = ("GitHub rejected the token. Update the GitHub token in the "
                "source-control sidebar.")
    elif "repository not found" in low or "not found" in low or "404" in low:
        hint = ("The remote repository wasn't found, or your token can't see it. "
                "Check the remote URL and the token's repository access.")
    else:
        hint = ""
    if hint:
        return f"{raw}\n\n{hint}" if raw else hint
    return raw or "git push failed"


# ── Request models ──

# ── Admin access control ──
# AuthMiddleware not active in main.py, so we read Authorization header directly.

_ADMIN_USER_ID = "admin"


def _get_user_id_from_request(request: Request) -> str:
    """Extract the caller's user_id (JWT, else open-mode bootstrap admin).

    Delegates to the shared request-based chokepoint so Source Control
    (commit/push, etc.) works over a Cloudflare Tunnel — where the browser can't
    mint a JWT — via the same open-mode fallback every other admin gate uses.
    See app.auth.identity.request_user_id.
    """
    return request_user_id(request)


def _require_admin(request: Request):
    """Check that the requesting user is admin. Raises 403 if not."""
    user_id = _get_user_id_from_request(request)
    if user_id != _ADMIN_USER_ID:
        raise HTTPException(
            status_code=403,
            detail="Restricted to admin users only.",
        )


# ── Request models ──

class CommitPushRequest(BaseModel):
    # All optional: a blank message means "auto-write one from the diff".
    message: str = Field("", max_length=2000)
    skip_push: bool = False
    include_untracked: bool = True
    # When true the endpoint streams granular progress events (NDJSON) instead of
    # returning a single result dict — drives the ⭐ button's live step display.
    stream: bool = False


class TokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


class RemoteUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=500)


class BranchRequest(BaseModel):
    branch: str = Field(..., min_length=1, max_length=200)


# Production-mirror request models (the "Release to production" flow).
class ProdConfigRequest(BaseModel):
    prod_folder: str | None = Field(None, max_length=1000)
    prod_remote_url: str | None = Field(None, max_length=500)
    auto_release_on_push: bool | None = None
    # The GitHub key is a secret → stored in the encrypted vault, NOT the local
    # config JSON. A blank string leaves the stored key unchanged (the UI never
    # echoes it back); only a non-empty value updates it.
    github_token: str | None = Field(None, max_length=500)


class ProdExcludeRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1000)
    excluded: bool = True


class ProdExcludeBulkRequest(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=5000)
    remove: list[str] = Field(default_factory=list, max_length=5000)


class ProdReleaseRequest(BaseModel):
    message: str = Field("", max_length=2000)
    stream: bool = False
    # Optional destination override sent by the File Explorer's folder field. When
    # present it is persisted to the config before the copy/push runs, so the
    # action always uses the freshest folder even if the field's debounced
    # auto-save hasn't landed yet.
    prod_folder: str | None = Field(None, max_length=1000)
    # Same backstop for the production repo (GitHub remote) field — an owner/repo
    # shorthand the engine expands to a full URL on save.
    prod_remote_url: str | None = Field(None, max_length=500)
    # And for the GitHub key field — persisted to the encrypted vault (blank =
    # keep) before the copy/push runs.
    github_token: str | None = Field(None, max_length=500)


# Multi-repo registry request models (the Source Control "repo selector").
class RepoAddRequest(BaseModel):
    label: str = Field("", max_length=200)
    folder: str = Field(..., min_length=1, max_length=1000)
    remote_url: str = Field("", max_length=500)
    token: str = Field("", max_length=500)


class RepoUpdateRequest(BaseModel):
    label: str | None = Field(None, max_length=200)
    folder: str | None = Field(None, max_length=1000)
    remote_url: str | None = Field(None, max_length=500)
    token: str | None = Field(None, max_length=500)


class RepoSelectRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)


# ── Endpoints ──

@router.get("/check-access")
async def check_access(request: Request):
    """Check if the current user has admin access to the GitHub features."""
    user_id = _get_user_id_from_request(request)
    return {"is_admin": user_id == _ADMIN_USER_ID}


@router.get("/status")
async def get_status(request: Request, fetch: bool = True):
    """Return repo status: branch, remote, file status, ahead/behind info.

    `fetch=1` (default) refreshes remote refs from origin first so ahead/behind
    is accurate, at the cost of a network round-trip. `fetch=0` skips that and
    returns the last-known local state instantly — used for the fast first paint
    of the Source Control panel, which then refreshes with `fetch=1` in the
    background.
    """
    # Point git at the user's selected repo (+ its key) before running commands
    _use_active_repo()

    # 0. Refresh remote refs so ahead/behind reflects what's actually on origin.
    # Without this, the cached refs are whatever was last fetched/pulled on this
    # machine, so the page reports "in sync" even when origin has new commits.
    # `--quiet` suppresses transcript noise; failures (offline, auth issue) are
    # tolerated — we fall through to whatever cached state we have. Skipped when
    # the caller asks for a local-only fast read (fetch=0).
    if fetch:
        _run_git(["fetch", "--quiet", "origin"], timeout=20)

    # 1. Branch name
    branch_out, _, rc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        raise HTTPException(status_code=500, detail="Not a git repository or no HEAD")
    branch = branch_out.strip()

    # 2. Remote URL
    remote_out, _, _ = _run_git(["remote", "get-url", "origin"])
    remote_url = remote_out.strip()

    # 3. Git status (short format)
    status_out, _, _ = _run_git(["status", "--short"])
    status_lines = [line.rstrip() for line in status_out.split("\n") if line.strip()]

    # 4. Parse status into staged / unstaged / untracked
    staged = []
    unstaged = []
    untracked = []
    for line in status_lines:
        index_flag = line[0] if len(line) > 0 else " "
        worktree_flag = line[1] if len(line) > 1 else " "
        path = line[3:] if len(line) > 3 else line
        if index_flag != " " and index_flag != "?":
            staged.append({"path": path, "flag": index_flag})
        if worktree_flag != " " and worktree_flag != "?":
            unstaged.append({"path": path, "flag": worktree_flag})
        if index_flag == "?" and worktree_flag == "?":
            untracked.append({"path": path})

    # 5. Recent commits — pull from origin/<branch> if remote exists, else HEAD.
    # Format: full-hash | short-hash | author-name | author-email | relative-date
    #         | iso-date | refs (decorations) | subject
    # Separator chosen to be unlikely in any field.
    _SEP = "\x1f"
    _log_format = _SEP.join(["%H", "%h", "%an", "%ae", "%ar", "%aI", "%D", "%s"])
    log_ref = f"origin/{branch}" if remote_url else "HEAD"
    # Show commits from remote tip back; also include local-only commits ahead of
    # remote by walking HEAD too (use --branches with explicit range fallback).
    # Simplest: get unique commits from both HEAD and origin/<branch>, newest first.
    log_revs = ["HEAD", log_ref] if remote_url else ["HEAD"]
    log_out, _, rc_log = _run_git(
        ["log", f"--format={_log_format}", "-30"] + log_revs,
        timeout=10,
    )

    # 5a. Build set of commits reachable from HEAD (= already pulled to this VM).
    head_hash = ""
    head_out, _, _ = _run_git(["rev-parse", "HEAD"], timeout=5)
    if head_out.strip():
        head_hash = head_out.strip()
    pulled_set: set[str] = set()
    rev_out, _, _ = _run_git(["rev-list", "-100", "HEAD"], timeout=10)
    if rev_out.strip():
        pulled_set = {h.strip() for h in rev_out.strip().split("\n") if h.strip()}

    commits = []
    seen_hashes: set[str] = set()
    if rc_log == 0:
        for line in log_out.split("\n"):
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split(_SEP)
            if len(parts) < 8:
                continue
            full_hash, short_hash, author, email, date_rel, date_iso, refs, subject = parts[:8]
            if full_hash in seen_hashes:
                continue
            seen_hashes.add(full_hash)
            commits.append({
                "hash": short_hash,
                "full_hash": full_hash,
                "author": author,
                "author_email": email,
                "date_relative": date_rel,
                "date_iso": date_iso,
                "refs": refs,
                "message": subject,
                "is_pulled": full_hash in pulled_set,
                "is_head": full_hash == head_hash,
            })

    # 6. Ahead/behind
    ahead_behind_out, _, _ = _run_git(
        ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"],
        timeout=10,
    )
    ahead, behind = 0, 0
    ab_parts = ahead_behind_out.strip().split()
    if len(ab_parts) == 2:
        ahead = int(ab_parts[0])
        behind = int(ab_parts[1])

    # 7. Has remote
    has_remote = bool(remote_url)

    # 8. Last commit
    last_commit_out, _, _ = _run_git(
        ["log", "-1", "--format=%H|%an|%ae|%ar|%s"],
        timeout=5,
    )
    last_commit = {}
    if last_commit_out.strip():
        parts = last_commit_out.strip().split("|", 4)
        if len(parts) == 5:
            last_commit = {
                "hash": parts[0],
                "author": parts[1],
                "author_email": parts[2],
                "date_relative": parts[3],
                "message": parts[4],
            }

    # 9. File count total
    file_count = len(staged) + len(unstaged) + len(untracked)

    return {
        "branch": branch,
        "remote_url": remote_url,
        "has_remote": has_remote,
        "ahead": ahead,
        "behind": behind,
        "file_count": file_count,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "commits": commits,
        "last_commit": last_commit,
    }


@router.get("/log-graph")
async def get_log_graph(request: Request, limit: int = 80, fetch: bool = True):
    """Return a commit graph across **all** local + remote branches.

    Output is shaped so the frontend can draw a VS Code style graph:
      - `commits` is a list newest-first; each row carries its `lane`
        (column index for the dot) and `routes` describing the line
        segments connecting it down to the next row.
      - `max_lane` is the total number of columns used (graph width).
      - `branches` is a map of refname -> short hash so the frontend can
        render branch tip labels.

    The endpoint is read-only and shows commits from every ref, including
    branches that aren't `main`, so users can see merges and side branches
    just like the VS Code Source Control graph.

    `fetch=1` (default) refreshes remote refs first so origin/* tips are live;
    `fetch=0` skips the network round-trip and graphs the last-known local refs
    instantly (used for the panel's fast first paint).
    """
    _use_active_repo()

    # Refresh remote refs so origin/* branch tips reflect what's on GitHub.
    # Skipped on a local-only fast read (fetch=0).
    if fetch:
        _run_git(["fetch", "--quiet", "--all"], timeout=20)

    # Clamp limit to a sane window — big graphs become unreadable anyway.
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 80

    # 1. Walk every ref's history and collect commits with parents.
    # %H = full sha, %P = parent shas (space-separated), %D = refnames,
    # %an = author, %ar = relative date, %aI = iso date, %s = subject
    _SEP = "\x1f"
    fmt = _SEP.join(["%H", "%P", "%D", "%an", "%ar", "%aI", "%s"])
    raw_out, _, rc = _run_git(
        ["log", "--all", "--date-order", f"--format={fmt}", f"-{limit}"],
        timeout=20,
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail="git log failed")

    raw_commits = []
    for line in raw_out.split("\n"):
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split(_SEP)
        if len(parts) < 7:
            continue
        full_hash, parents_str, refs, author, date_rel, date_iso, subject = parts[:7]
        parents = [p for p in parents_str.split(" ") if p]
        raw_commits.append({
            "full_hash": full_hash,
            "hash": full_hash[:7],
            "parents": parents,
            "refs": refs,
            "author": author,
            "date_relative": date_rel,
            "date_iso": date_iso,
            "message": subject,
        })

    # 2. HEAD hash + commits already pulled (= reachable from HEAD).
    head_hash = ""
    h_out, _, _ = _run_git(["rev-parse", "HEAD"], timeout=5)
    if h_out.strip():
        head_hash = h_out.strip()
    pulled_set: set[str] = set()
    rev_out, _, _ = _run_git(["rev-list", "-300", "HEAD"], timeout=10)
    if rev_out.strip():
        pulled_set = {h.strip() for h in rev_out.strip().split("\n") if h.strip()}

    # 2b. Commits that `git pull` would actually bring in — i.e. reachable
    # from origin/<current-branch> but not yet in HEAD. Commits on other
    # remote branches are NOT pullable from the current branch and should
    # not be marked with the "↓ not pulled" badge.
    pullable_set: set[str] = set()
    cur_branch_out, _, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    cur_branch = cur_branch_out.strip()
    if cur_branch and cur_branch != "HEAD":
        pull_out, _, pull_rc = _run_git(
            ["rev-list", "-300", f"origin/{cur_branch}", "^HEAD"],
            timeout=10,
        )
        if pull_rc == 0 and pull_out.strip():
            pullable_set = {h.strip() for h in pull_out.strip().split("\n") if h.strip()}

    # 2c. Commits committed locally but NOT yet on the remote — reachable from
    # HEAD but not from origin/<current-branch>. These are the "ahead" commits a
    # push would send; the frontend draws them with a dotted connector + green
    # halo so they read as not-yet-on-origin. If origin/<branch> doesn't resolve
    # (no upstream yet), the rev-list fails and we mark nothing — a safe default.
    unpushed_set: set[str] = set()
    if cur_branch and cur_branch != "HEAD":
        ahead_out, _, ahead_rc = _run_git(
            ["rev-list", "-300", "HEAD", f"^origin/{cur_branch}"],
            timeout=10,
        )
        if ahead_rc == 0 and ahead_out.strip():
            unpushed_set = {h.strip() for h in ahead_out.strip().split("\n") if h.strip()}

    # 3. Compute lanes (column positions) for the graph.
    # `active_lanes[i]` holds the hash of the commit expected to land in
    # lane `i` next (placed there by a child commit above). Walking the
    # log newest-first, for each commit:
    #   - snapshot lanes_in (what's coming into this row from above);
    #   - find its dot lane (first matching active slot, or open a new one
    #     if no child placed it — i.e. it's a branch tip);
    #   - free every active slot pointing at this commit (multiple
    #     children draw lines down to the same parent);
    #   - place its parents into lanes — first parent re-uses the dot's
    #     own lane when free (keeps the trunk straight), extras take new
    #     or recycled empty slots.
    active_lanes: list[str | None] = []
    graph_commits = []
    max_lane = 0

    def _first_index(hash_: str) -> int:
        for i, h in enumerate(active_lanes):
            if h == hash_:
                return i
        return -1

    def _open_empty_slot() -> int:
        for i, h in enumerate(active_lanes):
            if h is None:
                return i
        active_lanes.append(None)
        return len(active_lanes) - 1

    for c in raw_commits:
        # Snapshot BEFORE any mutation — these are the lines entering the
        # top edge of this row from rows above.
        lanes_in = list(active_lanes)

        lane = _first_index(c["full_hash"])
        if lane < 0:
            # Tip commit: no child placed it, claim an empty slot.
            lane = _open_empty_slot()

        # Free every slot waiting on this commit.
        for i, h in enumerate(active_lanes):
            if h == c["full_hash"]:
                active_lanes[i] = None

        # Assign parents to lanes.
        parent_lanes: list[int] = []
        for idx, p in enumerate(c["parents"]):
            existing = _first_index(p)
            if idx == 0 and active_lanes[lane] is None and (existing < 0 or existing > lane):
                # First parent = the trunk continuing straight down. Keep it in
                # this commit's OWN lane. If a side-branch row above already
                # parked the parent in a HIGHER lane (existing > lane), leave
                # that reservation in place too: the higher lane then carries a
                # merge line that bends into the parent's own row, while the
                # trunk stays put instead of veering right into the side
                # branch's column. Without this, the leftmost (e.g. main) trunk
                # would kink into a side lane the moment they share an ancestor.
                # (active_lanes[lane] is always free here — the dot's lane was
                # vacated just above.) A parent already sitting in a LOWER lane
                # (existing < lane) is the trunk we merge back into, so we fall
                # through and bend left toward it.
                active_lanes[lane] = p
                parent_lanes.append(lane)
            elif existing >= 0:
                parent_lanes.append(existing)
            else:
                slot = _open_empty_slot()
                active_lanes[slot] = p
                parent_lanes.append(slot)

        lanes_out = list(active_lanes)
        # Width = farthest non-empty lane this row touches.
        width_in = max((i + 1 for i, h in enumerate(lanes_in) if h is not None), default=0)
        width_out = max((i + 1 for i, h in enumerate(lanes_out) if h is not None), default=0)
        row_width = max(width_in, width_out, lane + 1)
        if row_width > max_lane:
            max_lane = row_width

        graph_commits.append({
            "hash": c["hash"],
            "full_hash": c["full_hash"],
            "parents": [p[:7] for p in c["parents"]],
            "refs": c["refs"],
            "author": c["author"],
            "date_relative": c["date_relative"],
            "date_iso": c["date_iso"],
            "message": c["message"],
            "lane": lane,
            "parent_lanes": parent_lanes,
            # Each element is the short hash the lane was tracking, or null
            # if the lane was empty. Frontend just checks truthy/falsy.
            "lanes_in":  [h[:7] if h else None for h in lanes_in],
            "lanes_out": [h[:7] if h else None for h in lanes_out],
            # When a child placed our hash in a non-dot lane (multi-child
            # commit) the frontend bends that line into our dot. Mark them.
            "merge_in_lanes": [i for i, h in enumerate(lanes_in)
                               if h == c["full_hash"] and i != lane],
            "is_head": c["full_hash"] == head_hash,
            "is_pulled": c["full_hash"] in pulled_set,
            "is_pullable": c["full_hash"] in pullable_set,
            "is_unpushed": c["full_hash"] in unpushed_set,
        })

    # 4. Map every ref name to its short hash for branch-tip badges.
    branches: dict[str, str] = {}
    ref_out, _, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short)\x1f%(objectname)", "refs/heads", "refs/remotes"],
        timeout=10,
    )
    for line in ref_out.split("\n"):
        line = line.strip()
        if not line:
            continue
        bits = line.split("\x1f")
        if len(bits) == 2:
            branches[bits[0]] = bits[1][:7]

    return {
        "commits": graph_commits,
        "max_lane": max_lane,
        "branches": branches,
        "current_branch": cur_branch,
        "head_hash": head_hash,
    }


@router.get("/commit/{commit_hash}")
async def get_commit_detail(commit_hash: str, request: Request):
    """Return full info about a single commit (body, files, diff stat)."""
    # Sanity-check hash format to avoid passing arbitrary args to git
    if not commit_hash or len(commit_hash) > 64 or not all(c in "0123456789abcdefABCDEF" for c in commit_hash):
        raise HTTPException(status_code=400, detail="Invalid commit hash")

    _use_active_repo()

    # Metadata + full body
    _SEP = "\x1f"
    fmt = _SEP.join(["%H", "%h", "%an", "%ae", "%ai", "%cn", "%ce", "%ci", "%P", "%s", "%b"])
    meta_out, meta_err, rc_meta = _run_git(
        ["show", "-s", f"--format={fmt}", commit_hash],
        timeout=10,
    )
    if rc_meta != 0:
        raise HTTPException(status_code=404, detail=meta_err.strip() or "Commit not found")
    parts = meta_out.rstrip("\n").split(_SEP)
    if len(parts) < 11:
        raise HTTPException(status_code=500, detail="Unexpected git show output")
    full_hash, short_hash, an, ae, ad, cn, ce, cd, parents, subject, body = parts[:11]

    # File-level diff stat (numstat: added, removed, path per line)
    stat_out, _, _ = _run_git(
        ["show", "--numstat", "--format=", commit_hash],
        timeout=15,
    )
    files = []
    for line in stat_out.split("\n"):
        line = line.strip()
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) >= 3:
            added = cols[0]
            removed = cols[1]
            path = cols[2]
            files.append({
                "path": path,
                "added": 0 if added == "-" else int(added),
                "removed": 0 if removed == "-" else int(removed),
                "binary": (added == "-" and removed == "-"),
            })

    return {
        "hash": short_hash,
        "full_hash": full_hash,
        "author": an,
        "author_email": ae,
        "author_date": ad,
        "committer": cn,
        "committer_email": ce,
        "commit_date": cd,
        "parents": [p for p in parents.split(" ") if p],
        "subject": subject,
        "body": body,
        "files": files,
    }


def _count_new_file_lines(path: Path, max_bytes: int = 2_000_000) -> int | None:
    """Line count of a brand-new (untracked) file, for its +N badge.

    `git diff` doesn't see untracked files, so for those we count the lines
    ourselves and treat them all as additions. Returns None — meaning "show no
    count" — for anything that shouldn't get a number: a binary file (a NUL byte
    near the top), a file too big to bother reading, or one that's gone /
    unreadable. An empty file is 0."""
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except Exception:  # noqa: BLE001
        return None
    if b"\x00" in data[:8192]:
        return None
    if not data:
        return 0
    # Trailing newline shouldn't count as an extra (empty) line.
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


@router.get("/line-stats")
async def get_line_stats(request: Request):
    """Per-file +/- line counts for everything changed since the last commit.

    Powers the green +N / red -N badges in the File Explorer tree. The returned
    map is keyed by **repo-relative path** and covers:
      • tracked edits — staged AND unstaged together, via `git diff --numstat
        HEAD`, so a badge reflects the whole working tree vs the last commit;
      • untracked files — counted as all-additions (a brand-new file reads +N).
    `--no-renames` keeps a rename as a delete (old path) + add (new path), so
    each side gets a clean per-path count instead of an `old => new` line to
    parse. `core.quotePath=false` stops git octal-escaping non-ASCII names so the
    paths match the tree's.

    Pinned to THIS app's own repo (the file tree's root) so the counts always
    line up with the tree even when Source Control has another repo selected.
    Read-only — the Files page is already admin-gated."""
    _pin_to_project_root()

    stats: dict[str, dict[str, int]] = {}

    # 1. Tracked changes vs HEAD (staged + unstaged combined). On a fresh repo
    #    with no commit yet this fails — tolerated; we still report untracked.
    out, _, rc = _run_git(
        ["-c", "core.quotePath=false", "diff", "--numstat", "--no-renames", "HEAD"],
        timeout=20,
    )
    if rc == 0:
        for line in out.split("\n"):
            line = line.rstrip("\n")
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            added, removed, path = cols[0], cols[1], cols[2]
            if added == "-" and removed == "-":
                continue  # binary — no meaningful line count
            stats[path] = {
                "added": int(added) if added.isdigit() else 0,
                "removed": int(removed) if removed.isdigit() else 0,
            }

    # 2. Untracked files — every line is an addition. `-uall` lists files inside
    #    untracked directories individually (not just the directory name). Capped
    #    so a huge accidentally-untracked tree can't make this read forever.
    st_out, _, _ = _run_git(
        ["-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all"],
        timeout=20,
    )
    root = _ACTIVE_REPO
    processed = 0
    for line in st_out.split("\n"):
        if not line.startswith("?? "):
            continue
        if processed >= 4000:
            break
        rel = line[3:].strip().strip('"')
        if not rel:
            continue
        added = _count_new_file_lines(root / rel)
        if added is not None:
            stats[rel] = {"added": added, "removed": 0}
        processed += 1

    return {"project_root": str(root).replace("\\", "/"), "stats": stats}


@router.post("/push")
async def push_to_remote(request: Request):
    """Push commits to the remote."""
    _require_admin(request)
    _use_active_repo()

    stdout, stderr, rc = _git_push(timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=_push_failure_hint(stderr, stdout))

    return {
        "status": "pushed",
        "message": "Push successful.",
        "output": stdout.strip(),
    }


# ── One-shot: stage → (auto-write message) → commit → push ──────────────────
#
# Shared core for the Source-Control ⭐ button (POST /commit-and-push below) AND
# the Git Control ability's `commit_and_push` agent tool (plugins/admin/
# source_tools.py delegates here). One source of truth so the instant UI path
# and the agent path can never drift in safety checks, message style, or flow.

# Obvious secret shapes we refuse to commit. Not exhaustive — a backstop, not a
# substitute for .gitignore / pre-commit scanning.
_SECRET_PATTERNS = [
    (r'ghp_[A-Za-z0-9]{36}', 'GitHub personal access token'),
    (r'sk-or-v1-[A-Za-z0-9]{32,}', 'OpenRouter/OpenAI API key'),
    (r'sk-[A-Za-z0-9]{32,}', 'API key pattern'),
    (r'AKIA[0-9A-Z]{16}', 'AWS access key'),
    (r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----', 'Private key'),
]


# How long we'll wait for the auto commit-message before giving up and using a
# generic fallback note. Kept short on purpose: the ⭐ button must feel instant,
# and the message is a nice-to-have (the user can always type their own).
_COMMIT_LLM_TIMEOUT = 20.0


def _commit_llm_client():
    """Lazy LLM client for commit-message writing — same provider config the
    chat/compaction path uses (LLM_BASE_URL / LLM_API_KEY, OpenRouter default).

    Bounded for responsiveness: a short timeout and **no automatic client-side
    retries**. The openai client retries twice by default with backoff, which
    turned a single slow/flaky call into a 90s+ stall — the real reason the ⭐
    button appeared to hang. With max_retries=0 a bad call fails once and
    returns immediately; the caller (`_generate_commit_message`) bounds the whole
    sub-call with asyncio.wait_for and drops to a deterministic fallback on any
    failure, so flakiness never re-introduces the stall."""
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENROUTER_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(base_url=base_url, api_key=api_key,
                           timeout=_COMMIT_LLM_TIMEOUT, max_retries=0)
    except ImportError:  # pragma: no cover
        from app.openai_compat import AsyncOpenAI
        return AsyncOpenAI(base_url=base_url, api_key=api_key,
                           timeout=_COMMIT_LLM_TIMEOUT)


def _changed_files(stat_out: str, untracked: list[str]) -> list[str]:
    """Pull the changed-file paths out of a `git diff --stat` block (each file
    line is ` path | N +++/---`; the trailing ` N files changed …` summary line
    has no `|` so it's skipped) and append the untracked paths, de-duplicated and
    order-preserving."""
    files: list[str] = []
    for ln in (stat_out or "").splitlines():
        if "|" in ln:
            path = ln.split("|", 1)[0].strip()
            if path:
                files.append(path)
    files.extend(untracked or [])
    seen: set[str] = set()
    uniq: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def _top_level_groups(files: list[str]) -> list[str]:
    """The top-level *area* for each path — the first path segment, or the bare
    filename for a root-level file — de-duplicated and order-preserving. Lets a
    many-file fallback name the areas that changed (`app, ui, docs`) when the
    individual paths are too long to list inside a 72-char title."""
    groups: list[str] = []
    seen: set[str] = set()
    for f in files:
        norm = f.replace("\\", "/").strip("/")
        head = norm.split("/", 1)[0] if "/" in norm else norm
        if head and head not in seen:
            seen.add(head)
            groups.append(head)
    return groups


def _fallback_commit_message(stat_out: str, untracked: list[str]) -> str:
    """A deterministic, *informative* commit message used whenever the LLM
    sub-call can't produce one (empty reply, timeout, API error).

    The title degrades gracefully so it stays meaningful instead of collapsing
    straight to a content-free count (the bug the user hit, where 3 long paths
    overflowed 72 chars and the title became a bare 'chore: update N files'):
      1. name the first few files outright (3 → 2), if they fit;
      2. else summarise by area — top-level dir/file — e.g. 'app, ui, docs';
      3. only as an absolute last resort, the bare count.
    The body always lists the actual files regardless of which title was chosen.
    """
    files = _changed_files(stat_out, untracked)
    if not files:
        return "chore: staged changes"
    if len(files) == 1:
        return f"chore: update {files[0]}"

    title = ""
    # 1) Name the first few files outright, if they fit the 72-char title.
    for n in (3, 2):
        if n > len(files):
            continue
        shown = ", ".join(files[:n])
        extra = len(files) - n
        candidate = f"chore: update {shown}"
        if extra > 0:
            candidate += f" and {extra} more file{'s' if extra != 1 else ''}"
        if len(candidate) <= 72:
            title = candidate
            break
    # 2) Paths too long to name — summarise by area instead of a bare count.
    if not title:
        groups = _top_level_groups(files)
        for g in (3, 2, 1):
            shown = ", ".join(groups[:g])
            candidate = f"chore: update {shown} ({len(files)} files)"
            if len(candidate) <= 72:
                title = candidate
                break
    # 3) Absolute last resort.
    if not title:
        title = f"chore: update {len(files)} files"

    body_files = files[:20]
    body = "\n".join(f"- {f}" for f in body_files)
    if len(files) > len(body_files):
        body += f"\n- ... and {len(files) - len(body_files)} more"
    return f"{title}\n\n{body}"


async def _generate_commit_message(stat_out: str, full_diff: str,
                                   untracked: list[str]) -> tuple[str, str, str]:
    """Write a conventional-commit message from the diff via a lightweight LLM
    sub-call. Returns ``(message, source, detail)``: ``source`` is ``"llm"`` when
    the model produced the note, or ``"fallback"`` (with ``detail`` = why) when it
    dropped to the deterministic file-list summary. The fallback is still a real,
    informative message — ``source`` just lets the caller TELL the user the
    auto-writer was skipped (the ⭐ progress UI shows "auto-message unavailable")."""
    fallback = _fallback_commit_message(stat_out, untracked)
    model = (
        os.environ.get("LLM_MODEL")
        or os.environ.get("OPENROUTER_MODEL")
        or "deepseek/deepseek-v4-flash"
    )
    diff_window = full_diff[:6000] if full_diff else "(no diff)"
    if full_diff and len(full_diff) > 6000:
        diff_window += f"\n... (truncated, {len(full_diff)} total bytes)"
    summary = (
        f"Changed files:\n{stat_out.strip() or 'N/A'}\n\n"
        "Untracked:\n"
        + ("\n".join(f"  {f}" for f in untracked) if untracked else "  (none)")
        + f"\n\nDiff:\n{diff_window}"
    )
    # One bounded attempt. The ⭐ button must feel instant, so the whole sub-call
    # is capped with asyncio.wait_for and we fall straight through to the
    # deterministic file-list fallback on ANY failure — timeout, empty reply, or a
    # connection blip. (No retry loop: retrying risks the very stall the timeout
    # exists to prevent, and the fallback is already a real, informative message.)
    try:
        client = _commit_llm_client()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "You generate concise git commit messages from a diff. "
                        "Output ONLY:\n"
                        "Line 1: conventional commit title (max 72 chars)\n"
                        "Line 2: blank\n"
                        "Line 3+: brief bullet-point body\n"
                        "Types: feat, fix, chore, refactor, docs, style, test, perf, ci"
                    )},
                    {"role": "user", "content": f"Generate a commit message for:\n\n{summary}"},
                ],
                max_tokens=300,
                temperature=0.3,
            ),
            timeout=_COMMIT_LLM_TIMEOUT,
        )
        try:
            _u = getattr(resp, "usage", None)
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model,
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label="commit",
                )
        except Exception:
            pass
        content = (resp.choices[0].message.content or "").strip()
        if content:
            return content, "llm", ""
        logger.warning("LLM commit-message: empty reply (model=%s) — using file-list fallback", model)
        return fallback, "fallback", "empty reply"
    except Exception as e:
        reason = str(e) or type(e).__name__
        logger.warning("LLM commit-message sub-call failed (%s) — using file-list fallback", reason)
        return fallback, "fallback", reason


async def _commit_and_push_events(message: str = "", *, skip_push: bool = False,
                                  include_untracked: bool = True):
    """Run the one-shot stage → commit → push and **yield a progress event before
    each phase**, so the UI can show granular, *honest* status (analysing →
    safety-check → writing message → committing → pushing) rather than one opaque
    spinner.

    Each event is a dict with a ``phase`` key; the FINAL event is always
    ``{"phase": "done", "result": <dict>}`` carrying the same structured result
    (status one of committed / nothing_to_commit / blocked / error) that callers
    expect. Phase labels are deliberately generic — the frontend owns the
    user-facing wording.

    This is the single source of truth: ``commit_and_push_repo`` (the agent tool +
    non-streaming endpoint) just drains it for the final result, and the streaming
    endpoint forwards every event — so the ⭐ button and the agent can't drift.

    The caller MUST point git at the right repo first: the UI route calls
    `_use_active_repo()` (the user's selected repo + key), the agent ability calls
    `_pin_to_project_root()` (always this app). We deliberately don't reset the
    token here so a non-default repo's key isn't clobbered back to the shared one.
    """
    # Every git step below runs through `asyncio.to_thread` so the blocking
    # `subprocess.run` happens on a worker thread, NOT the event loop. This is what
    # makes the NDJSON progress actually STREAM: a yielded phase event is only
    # flushed to the client when the loop gets a turn, and a synchronous git call
    # on the loop would hold it hostage for the whole commit+push — so the user saw
    # "Starting…" and then nothing until the (often slow) push finished. Running git
    # off-loop lets each step's event reach the browser the moment it's produced.
    async def _git(args, timeout=15):
        return await asyncio.to_thread(_run_git, args, timeout=timeout)

    # 1. Repo state — bail early if clean.
    yield {"phase": "analyzing"}
    await asyncio.sleep(0)  # hand the loop a turn so the event flushes now
    status_out, status_err, status_code = await _git(["status", "--porcelain"], timeout=30)
    if status_code != 0:
        yield {"phase": "done", "result": {
            "status": "error", "message": f"git status failed: {status_err.strip()}"}}
        return
    if not status_out.strip():
        yield {"phase": "done", "result": {
            "status": "nothing_to_commit",
            "message": "Nothing to commit — working tree is clean."}}
        return

    # 2. Diffs (stat for the summary, full diff for the secret scan + LLM).
    #    Diff against HEAD, not the bare working tree, so ALREADY-STAGED changes
    #    are included — otherwise a pre-staged tree looks empty here and the
    #    auto-message/secret-scan see "(no diff)" while the commit below still
    #    captures everything. Fall back to the working-tree diff if there is no
    #    HEAD yet (brand-new repo with no commits).
    stat_out, _, stat_rc = await _git(["diff", "HEAD", "--stat"], timeout=30)
    full_diff, _, diff_rc = await _git(["diff", "HEAD"], timeout=30)
    if stat_rc != 0:
        stat_out, _, _ = await _git(["diff", "--stat"], timeout=30)
    if diff_rc != 0:
        full_diff, _, _ = await _git(["diff"], timeout=30)
    untracked = [ln.strip()[3:] for ln in status_out.strip().splitlines()
                 if ln.strip().startswith("?? ")]
    file_count = len(status_out.strip().splitlines())

    # 3. Safety: refuse to commit obvious secrets.
    yield {"phase": "scanning", "file_count": file_count}
    await asyncio.sleep(0)
    for pattern, label in _SECRET_PATTERNS:
        if re.search(pattern, full_diff or ""):
            yield {"phase": "done", "result": {
                "status": "blocked",
                "message": (f"Possible {label} found in the diff — commit aborted. "
                            "Remove it from the working tree and retry.")}}
            return

    # 4. Commit message: use the caller's, else auto-write one.
    if message and message.strip():
        commit_msg = message.strip()
        yield {"phase": "message", "source": "user",
               "title": commit_msg.split("\n")[0].strip()}
        await asyncio.sleep(0)
    else:
        yield {"phase": "message", "source": "auto"}
        await asyncio.sleep(0)
        commit_msg, msg_source, msg_detail = await _generate_commit_message(
            stat_out, full_diff, untracked)
        title = commit_msg.split("\n")[0].strip()
        if msg_source == "fallback":
            # Non-fatal: the LLM auto-writer couldn't produce a note, so we used
            # the deterministic file-list summary. Tell the user what happened.
            yield {"phase": "message_fallback", "title": title, "detail": msg_detail}
        else:
            yield {"phase": "message_ready", "title": title}
        await asyncio.sleep(0)

    # 5. Stage.
    yield {"phase": "committing"}
    await asyncio.sleep(0)
    _, add_err, add_code = await _git(["add", "-A" if include_untracked else "-u"], timeout=30)
    if add_code != 0:
        yield {"phase": "done", "result": {
            "status": "error", "message": f"git add failed: {add_err.strip()}"}}
        return

    # 6. Commit (via -F file so a multi-line body survives).
    msg_file = _PROJECT_ROOT / "data" / "tmp" / "_commit_msg.txt"
    msg_file.parent.mkdir(parents=True, exist_ok=True)
    msg_file.write_text(commit_msg, encoding="utf-8")
    _, commit_err, commit_code = await _git(["commit", "-F", str(msg_file)], timeout=30)
    try:
        msg_file.unlink()
    except OSError:
        pass
    if commit_code != 0:
        yield {"phase": "done", "result": {
            "status": "error", "message": f"git commit failed: {commit_err.strip()}"}}
        return

    head_out, _, _ = await _git(["rev-parse", "HEAD"], timeout=10)
    commit_hash = head_out.strip()[:12]
    commit_title = commit_msg.split("\n")[0].strip()

    # 7. Push (unless asked to hold off).
    if skip_push:
        push = {"attempted": False, "ok": None, "detail": "Skipped (commit only)."}
    else:
        yield {"phase": "pushing"}
        await asyncio.sleep(0)
        push_out, push_err, push_code = await asyncio.to_thread(_git_push, timeout=120)
        push = {"attempted": True, "ok": push_code == 0,
                "detail": "Push successful." if push_code == 0
                else _push_failure_hint(push_err, push_out)}

    yield {"phase": "done", "result": {
        "status": "committed",
        "message_used": commit_msg,
        "title": commit_title,
        "hash": commit_hash,
        "push": push,
        "stat": stat_out.strip(),
        "file_count": file_count,
    }}


async def commit_and_push_repo(message: str = "", *, skip_push: bool = False,
                               include_untracked: bool = True) -> dict:
    """Stage all → (auto-write message if blank) → commit → push, in one shot.

    Returns a structured dict (status one of: committed / nothing_to_commit /
    blocked / error). Never raises for ordinary git failures — the caller decides
    how to surface them. Thin drain over ``_commit_and_push_events`` (the single
    source of truth): used by the Git Control agent tool and the non-streaming
    endpoint; the streaming endpoint forwards the generator's events directly.
    """
    result = {"status": "error", "message": "commit+push produced no result"}
    async for ev in _commit_and_push_events(
            message, skip_push=skip_push, include_untracked=include_untracked):
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result


@router.post("/commit-and-push")
async def commit_and_push(req: CommitPushRequest, request: Request):
    """One-shot: stage all → auto-write a commit message (unless one is supplied)
    → commit → push. Backs the Source-Control ⭐ button so a click commits and
    pushes immediately instead of handing the job to a chat agent.

    With ``stream: true`` it returns a newline-delimited JSON (NDJSON) stream of
    progress events — ``{"phase": "analyzing"|"scanning"|"message"|"committing"|
    "pushing", …}`` and a final ``{"phase": "done", "result": {…}}`` — so the ⭐
    button can show granular live status. Without it, it runs to completion and
    returns the single result dict (back-compat; also the agent-tool behaviour).
    Errors in the streaming path arrive INSIDE the final ``done`` event (the UI
    shows them inline) rather than as an HTTP error code.
    """
    _require_admin(request)
    # Commit + push the repo the user currently has selected (with its own key).
    _use_active_repo()

    if req.stream:
        async def _event_stream():
            try:
                async for ev in _commit_and_push_events(
                        req.message or "",
                        skip_push=req.skip_push,
                        include_untracked=req.include_untracked):
                    yield json.dumps(ev) + "\n"
            except Exception as e:  # never leave the client's reader hanging
                logger.exception("commit-and-push stream crashed")
                yield json.dumps({"phase": "done", "result": {
                    "status": "error",
                    "message": str(e) or "commit+push failed"}}) + "\n"
        # no-cache + X-Accel-Buffering tell intermediaries (nginx, tunnels) NOT to
        # buffer the response, so the per-phase NDJSON lines arrive live instead of
        # all-at-once at the end.
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    result = await commit_and_push_repo(
        req.message or "",
        skip_push=req.skip_push,
        include_untracked=req.include_untracked,
    )
    status = result.get("status")
    if status == "blocked":
        raise HTTPException(status_code=400, detail=result.get("message"))
    if status == "error":
        raise HTTPException(status_code=500, detail=result.get("message"))
    return result


def _is_backend_file(path: str) -> bool:
    """Heuristic: does this path touch code that requires a server restart?

    Anything under app/ or scripts/, plus root-level Python files, the
    requirements/lock files, and the FastAPI entrypoint. Frontend assets
    (ui/, index.html, README, docs) are picked up by a browser refresh.
    """
    if not path:
        return False
    p = path.strip()
    backend_dirs = ("app/", "data/", "scripts/", "migrations/", "supabase/", "tests/")
    if p.startswith(backend_dirs):
        return True
    backend_root_files = (
        "run.py", "pyproject.toml", "uv.lock", "requirements.txt",
    )
    return p in backend_root_files


def _head_hash() -> str:
    out, _, _ = _run_git(["rev-parse", "HEAD"], timeout=5)
    return out.strip()


def _files_changed_between(old: str, new: str) -> list[str]:
    """git diff --name-only old..new — empty list on error/no diff."""
    if not old or not new or old == new:
        return []
    out, _, rc = _run_git(["diff", "--name-only", f"{old}..{new}"], timeout=10)
    if rc != 0:
        return []
    return [line.strip() for line in out.split("\n") if line.strip()]


@router.post("/pull")
async def pull_from_remote(request: Request):
    """Pull from remote. Returns whether backend files changed so the
    caller can decide to restart the server.
    """
    _require_admin(request)
    _use_active_repo()

    before = _head_hash()
    stdout, stderr, rc = _run_git(["pull"], timeout=30)
    if rc != 0:
        detail = stderr.strip()
        if "Authentication failed" in stderr or "could not read" in stderr:
            detail += "\n\nSet your GitHub token in the File Manager sidebar (source-control view)."
        raise HTTPException(status_code=500, detail=detail)
    after = _head_hash()

    files = _files_changed_between(before, after)
    backend_changed = any(_is_backend_file(f) for f in files)

    return {
        "status": "pulled",
        "message": "Pull successful." if before != after else "Already up to date.",
        "output": stdout.strip(),
        "before": before[:7],
        "after": after[:7],
        "files_changed": files,
        "backend_changed": backend_changed,
    }


@router.get("/branches")
async def list_branches(request: Request):
    """List all branches the user can switch to or merge.

    Returns local branches plus remote-tracking branches (origin/*). For
    each, includes the short hash and whether a local checkout exists.
    """
    _require_admin(request)

    # Local branches
    local_out, _, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short)\x1f%(objectname)", "refs/heads"],
        timeout=10,
    )
    local: dict[str, str] = {}
    for line in local_out.split("\n"):
        bits = line.strip().split("\x1f")
        if len(bits) == 2:
            local[bits[0]] = bits[1]

    # Remote branches
    remote_out, _, _ = _run_git(
        ["for-each-ref", "--format=%(refname:short)\x1f%(objectname)", "refs/remotes/origin"],
        timeout=10,
    )
    remotes: dict[str, str] = {}
    for line in remote_out.split("\n"):
        bits = line.strip().split("\x1f")
        if len(bits) == 2 and bits[0] != "origin/HEAD":
            # Strip the leading "origin/" to get the bare branch name
            bare = bits[0][len("origin/"):] if bits[0].startswith("origin/") else bits[0]
            remotes[bare] = bits[1]

    # Build the union
    names = sorted(set(local.keys()) | set(remotes.keys()))
    branches = []
    for name in names:
        branches.append({
            "name": name,
            "hash": (local.get(name) or remotes.get(name, ""))[:7],
            "local": name in local,
            "remote": name in remotes,
        })

    cur_out, _, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    return {"current": cur_out.strip(), "branches": branches}


def _working_tree_dirty() -> tuple[bool, list[str]]:
    """Returns (dirty, list-of-paths). Empty list when clean."""
    out, _, _ = _run_git(["status", "--porcelain"], timeout=10)
    paths = [line[3:] if len(line) > 3 else line for line in out.split("\n") if line.strip()]
    return (bool(paths), paths)


@router.post("/checkout")
async def checkout_branch(req: BranchRequest, request: Request):
    """Switch the working tree to a different branch.

    Refuses if the working tree has uncommitted changes (would clobber).
    If the requested branch only exists on the remote, creates a local
    tracking branch from origin/<name>.
    """
    _require_admin(request)
    _use_active_repo()

    branch = req.branch.strip()
    if not branch:
        raise HTTPException(status_code=400, detail="Branch name required.")

    dirty, dirty_paths = _working_tree_dirty()
    if dirty:
        raise HTTPException(
            status_code=409,
            detail=(
                "Working tree has uncommitted changes. Commit or discard them before switching branches.\n"
                + "\n".join(dirty_paths[:10])
                + ("\n…" if len(dirty_paths) > 10 else "")
            ),
        )

    # Local branch exists → straight checkout.
    # Otherwise, create a tracking branch from origin/<name>.
    _, _, rc_local = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], timeout=5)

    before = _head_hash()
    if rc_local == 0:
        stdout, stderr, rc = _run_git(["checkout", branch], timeout=20)
    else:
        # Verify the remote exists before trying
        _, _, rc_remote = _run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"], timeout=5)
        if rc_remote != 0:
            raise HTTPException(status_code=404, detail=f"Branch '{branch}' not found locally or on origin.")
        stdout, stderr, rc = _run_git(["checkout", "-b", branch, f"origin/{branch}"], timeout=20)

    if rc != 0:
        raise HTTPException(status_code=500, detail=stderr.strip() or "git checkout failed")

    after = _head_hash()
    files = _files_changed_between(before, after)
    backend_changed = any(_is_backend_file(f) for f in files)

    return {
        "status": "checked-out",
        "branch": branch,
        "message": f"Switched to {branch}.",
        "files_changed": files,
        "backend_changed": backend_changed,
    }


@router.post("/merge")
async def merge_branch(req: BranchRequest, request: Request):
    """Merge another branch into the current branch.

    Aborts on conflict (no half-merged state left behind) and reports the
    conflicting files so the caller can show them.
    """
    _require_admin(request)
    _use_active_repo()

    branch = req.branch.strip()
    if not branch:
        raise HTTPException(status_code=400, detail="Branch name required.")

    dirty, dirty_paths = _working_tree_dirty()
    if dirty:
        raise HTTPException(
            status_code=409,
            detail=(
                "Working tree has uncommitted changes. Commit or discard them before merging.\n"
                + "\n".join(dirty_paths[:10])
                + ("\n…" if len(dirty_paths) > 10 else "")
            ),
        )

    # If the branch only exists on the remote, merge origin/<name> directly.
    _, _, rc_local = _run_git(["show-ref", "--verify", f"refs/heads/{branch}"], timeout=5)
    if rc_local == 0:
        merge_ref = branch
    else:
        _, _, rc_remote = _run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"], timeout=5)
        if rc_remote != 0:
            raise HTTPException(status_code=404, detail=f"Branch '{branch}' not found locally or on origin.")
        merge_ref = f"origin/{branch}"

    before = _head_hash()
    stdout, stderr, rc = _run_git(
        ["merge", "--no-edit", merge_ref],
        timeout=30,
    )
    if rc != 0:
        # Try to roll back so we don't leave a partial merge for them to clean up
        _run_git(["merge", "--abort"], timeout=10)
        # Detect conflicts (this catches the common reason)
        conflict_out, _, _ = _run_git(["diff", "--name-only", "--diff-filter=U"], timeout=5)
        conflicts = [l.strip() for l in conflict_out.split("\n") if l.strip()]
        detail = stderr.strip() or stdout.strip() or "merge failed"
        if conflicts:
            detail = "Merge conflicts in:\n" + "\n".join(conflicts) + "\n\nMerge aborted — resolve conflicts in a terminal and try again."
        raise HTTPException(status_code=409, detail=detail)

    after = _head_hash()
    files = _files_changed_between(before, after)
    backend_changed = any(_is_backend_file(f) for f in files)

    return {
        "status": "merged",
        "branch": branch,
        "message": (
            f"Merged {branch} into HEAD." if before != after
            else f"Already up to date with {branch}."
        ),
        "output": stdout.strip(),
        "before": before[:7],
        "after": after[:7],
        "files_changed": files,
        "backend_changed": backend_changed,
    }


@router.post("/token")
async def set_token(req: TokenRequest, request: Request):
    """Store a GitHub personal access token for HTTPS auth.
    Single shared token — same for all users (the repo is shared).
    """
    _require_admin(request)
    _save_token(req.token)
    _cache_token(req.token)
    logger.info("GitHub token saved")
    return {"status": "ok", "message": "GitHub token saved."}


@router.get("/token-status")
async def token_status():
    """Check if a GitHub token is configured."""
    token = _get_token()
    return {
        "configured": bool(token),
        "masked": f"{token[:4]}****" if len(token) > 8 else ("****" if token else ""),
    }


@router.post("/remote-url")
# WHEN-REMOTE-URL-EDIT: Sets the git remote origin URL. Called from source
# control sidebar when the user double-clicks or long-presses the remote URL
# display and submits a new value. Runs `git remote set-url origin <url>`.
# Nav: ui/js/files-git.js wireEvents() → commitEditRemote() → POST this endpoint.
async def set_remote_url(req: RemoteUrlRequest, request: Request):
    """Change the git remote URL for origin (on the active repo)."""
    _require_admin(request)
    _use_active_repo()
    stdout, stderr, rc = _run_git(["remote", "set-url", "origin", req.url], timeout=10)
    if rc != 0:
        raise HTTPException(status_code=500, detail=stderr.strip() or "Failed to set remote URL")
    # Keep the registry's stored remote in sync when a non-built-in repo is active,
    # so the saved entry and the repo's git config don't drift apart.
    try:
        active = git_repos.get_active_full()
        if not active.get("builtin"):
            git_repos.update_repo(active["id"], remote_url=req.url)
    except Exception:  # noqa: BLE001
        pass
    logger.info("Remote origin URL changed to %s", req.url)
    return {"status": "ok", "message": "Remote URL updated.", "remote_url": req.url}


# ── Repositories (the Source Control multi-repo selector) ────────────────────
# The Git page can manage MORE than this app's own repo: point it at any local git
# folder paired with its own GitHub remote + key, and the Changes list + commit
# graph reflect THAT repo. The built-in webAgent entry is always present and can't
# be removed; selecting it behaves exactly like before. Heavy lifting lives in
# app/git_repos.py. Selecting a repo re-points every git action on the page via
# `_use_active_repo()` (above). Route order matters: the literal `/repos/select`
# must precede the `/repos/{repo_id}` param route so "select" isn't swallowed as an id.

@router.get("/repos")
async def list_git_repos(request: Request):
    """All configured repos (webAgent first), each with active/builtin flags. The
    raw key is never returned — only a ``has_token`` flag."""
    _require_admin(request)
    return {"repos": git_repos.list_repos()}


@router.get("/repos/validate")
async def validate_git_repo(request: Request, path: str = ""):
    """Check a folder is a git repo; return its branch + origin so the Add form can
    pre-fill them and warn early on a non-repo folder."""
    _require_admin(request)
    return git_repos.validate_folder(path)


@router.post("/repos")
async def add_git_repo(req: RepoAddRequest, request: Request):
    """Register a new local repo (folder + GitHub remote + key)."""
    _require_admin(request)
    try:
        entry = git_repos.add_repo(
            label=req.label, folder=req.folder,
            remote_url=req.remote_url, token=req.token,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "repo": entry, "repos": git_repos.list_repos()}


@router.post("/repos/select")
async def select_git_repo(req: RepoSelectRequest, request: Request):
    """Choose which repo the Git page operates on."""
    _require_admin(request)
    try:
        git_repos.set_active(req.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Re-point immediately so a follow-up status/graph call uses the new repo.
    _use_active_repo()
    return {"status": "ok", "repos": git_repos.list_repos()}


@router.post("/repos/{repo_id}")
async def update_git_repo(repo_id: str, req: RepoUpdateRequest, request: Request):
    """Edit a registered repo. A blank token keeps the existing key."""
    _require_admin(request)
    try:
        entry = git_repos.update_repo(
            repo_id, label=req.label, folder=req.folder,
            remote_url=req.remote_url, token=req.token,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "repo": entry, "repos": git_repos.list_repos()}


@router.delete("/repos/{repo_id}")
async def delete_git_repo(repo_id: str, request: Request):
    """Forget a registered repo (the built-in webAgent repo can't be removed)."""
    _require_admin(request)
    try:
        git_repos.remove_repo(repo_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _use_active_repo()
    return {"status": "ok", "repos": git_repos.list_repos()}


# ── Production mirror (dual-repo "Release to production") ────────────────────
# This dev repo can publish a TRIMMED copy of itself — everything except the
# folders an admin marked dev-only in the File Explorer — to a SEPARATE sibling
# production repo with its own GitHub remote. Git can't push "everything except
# folder X" (a commit carries the whole tree), so production is its own repo with
# its own history, regenerated on demand. The heavy lifting lives in
# app/production_mirror.py; these endpoints are the thin admin surface. The File
# Explorer reads/writes the exclude list through the same endpoints, so the Git
# page and the explorer share one source of truth.

@router.get("/production/config")
async def get_production_config(request: Request):
    """Production folder path, remote URL, exclude list, last-release record, plus
    a ``token_set`` flag (whether a GitHub key is stored — never the key itself)."""
    _require_admin(request)
    cfg = production_mirror.load_config()
    cfg["token_set"] = await production_mirror.token_is_set()
    return cfg


@router.post("/production/config")
async def set_production_config(req: ProdConfigRequest, request: Request):
    """Update the production folder path / remote URL / auto-release toggle, and
    (separately) the GitHub key into the encrypted vault. A blank key is ignored
    so it's never wiped by a save that didn't touch it."""
    _require_admin(request)
    cfg = production_mirror.save_config({
        "prod_folder": req.prod_folder,
        "prod_remote_url": req.prod_remote_url,
        "auto_release_on_push": req.auto_release_on_push,
    })
    if req.github_token:
        await production_mirror.save_vault_token(req.github_token)
    token_set = await production_mirror.token_is_set()
    return {"status": "ok", "config": cfg, "token_set": token_set}


@router.get("/production/exclude")
async def get_production_exclude(request: Request):
    """The exclude list (repo-relative folders) + the project root, so the File
    Explorer can map them to the absolute paths its rows carry."""
    _require_admin(request)
    cfg = production_mirror.load_config()
    return {
        "exclude_paths": cfg["exclude_paths"],
        "project_root": str(_PROJECT_ROOT).replace("\\", "/"),
    }


@router.post("/production/exclude")
async def set_production_exclude(req: ProdExcludeRequest, request: Request):
    """Mark one folder dev-only (or include it again). Shared by both pages."""
    _require_admin(request)
    try:
        cfg = production_mirror.set_excluded(req.path, req.excluded)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "exclude_paths": cfg["exclude_paths"]}


@router.post("/production/exclude-bulk")
async def set_production_exclude_bulk(req: ProdExcludeBulkRequest, request: Request):
    """Apply many exclude-list changes at once (add + remove). Used by the File
    Explorer's tri-state folder checkbox to include/exclude a whole subtree in a
    single atomic write."""
    _require_admin(request)
    cfg = production_mirror.set_excluded_bulk(add=req.add, remove=req.remove)
    return {"status": "ok", "exclude_paths": cfg["exclude_paths"]}


@router.get("/production/status")
async def get_production_status(request: Request):
    """Lightweight status for the Production section of the Git page."""
    _require_admin(request)
    return production_mirror.production_status()


@router.get("/production/diff")
async def get_production_diff(request: Request):
    """How dev and the production folder differ right now — the added/updated/
    removed counts a one-way Sync would apply. Read-only; powers the File
    Explorer's More-menu 'in sync / N changes' line. Walks + byte-compares the
    shipping set, so it runs in a worker thread to keep the event loop free."""
    _require_admin(request)
    import asyncio
    return await asyncio.to_thread(production_mirror.diff_status)


@router.post("/production/release")
async def release_to_production(req: ProdReleaseRequest, request: Request):
    """Regenerate the trimmed copy in the production folder, commit, and push it
    to the production GitHub remote. Streams NDJSON progress when stream=true."""
    _require_admin(request)

    if req.stream:
        async def _event_stream():
            try:
                async for ev in production_mirror.release_events(req.message or ""):
                    yield json.dumps(ev) + "\n"
            except Exception as e:  # noqa: BLE001
                logger.exception("release-to-production stream crashed")
                yield json.dumps({"phase": "done", "result": {
                    "status": "error",
                    "message": str(e) or "release failed"}}) + "\n"
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    result = {"status": "error", "message": "release produced no result"}
    async for ev in production_mirror.release_events(req.message or ""):
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result


async def _persist_prod_overrides(req: "ProdReleaseRequest") -> None:
    """Persist the folder + remote + key a Sync/Push request carries (the File
    Explorer's More-menu fields) before the action runs, so it always uses the
    freshest values even if the field's debounced auto-save hasn't landed yet.
    The folder + remote go to the local config; the GitHub key (if any) goes to
    the encrypted vault."""
    overrides = {}
    if req.prod_folder:
        overrides["prod_folder"] = req.prod_folder
    if req.prod_remote_url:
        overrides["prod_remote_url"] = req.prod_remote_url
    if overrides:
        production_mirror.save_config(overrides)
    if req.github_token:
        await production_mirror.save_vault_token(req.github_token)


@router.post("/production/copy")
async def copy_to_production(req: ProdReleaseRequest, request: Request):
    """First half of a release: build the trimmed copy in the production folder
    and commit it there LOCALLY (no push). The File Explorer's "Sync to
    production" calls this. Streams NDJSON progress when stream=true."""
    _require_admin(request)
    await _persist_prod_overrides(req)

    if req.stream:
        async def _event_stream():
            try:
                async for ev in production_mirror.copy_events(req.message or ""):
                    yield json.dumps(ev) + "\n"
            except Exception as e:  # noqa: BLE001
                logger.exception("copy-to-production stream crashed")
                yield json.dumps({"phase": "done", "result": {
                    "status": "error",
                    "message": str(e) or "copy failed"}}) + "\n"
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    result = {"status": "error", "message": "copy produced no result"}
    async for ev in production_mirror.copy_events(req.message or ""):
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result


@router.post("/production/push")
async def push_to_production(req: ProdReleaseRequest, request: Request):
    """Second half of a release: push the production folder's local commit to its
    GitHub remote. The File Explorer's "Push to GitHub" calls this. Streams NDJSON
    progress when stream=true. (The request's ``message`` is ignored here.)"""
    _require_admin(request)
    await _persist_prod_overrides(req)

    if req.stream:
        async def _event_stream():
            try:
                async for ev in production_mirror.push_events():
                    yield json.dumps(ev) + "\n"
            except Exception as e:  # noqa: BLE001
                logger.exception("push-to-production stream crashed")
                yield json.dumps({"phase": "done", "result": {
                    "status": "error",
                    "message": str(e) or "push failed"}}) + "\n"
        return StreamingResponse(
            _event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    result = {"status": "error", "message": "push produced no result"}
    async for ev in production_mirror.push_events():
        if ev.get("phase") == "done":
            result = ev.get("result", result)
    return result
