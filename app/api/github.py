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

import logging
import os
import subprocess
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github")

# ── Find project root (parent of app/) ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Token storage (single shared token in provider.json) ──
_TOKEN_FILE = _PROJECT_ROOT / "provider.json"


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


def _run_git(args: list[str], timeout: int = 15) -> tuple[str, str, int]:
    """Run a git command in the project root. Returns (stdout, stderr, returncode).
    Uses cached token for HTTPS auth if available.
    """
    env = os.environ.copy()
    token = _TOKEN_CACHE or _get_token()
    if token:
        env["GIT_USERNAME"] = "token"
        env["GIT_PASSWORD"] = token
        env["GIT_ASKPASS"] = ""
        env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(_PROJECT_ROOT),
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


# ── Request models ──

# ── Admin access control ──
# AuthMiddleware not active in main.py, so we read Authorization header directly.

_ADMIN_USER_ID = "admin_default"


def _get_user_id_from_request(request: Request) -> str:
    """Extract user_id from the Authorization header (JWT). Returns empty if not auth'd."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        payload = decode_token(token)
        if payload:
            return payload.get("user_id", "")
    return ""


def _require_admin(request: Request):
    """Check that the requesting user is admin. Raises 403 if not."""
    user_id = _get_user_id_from_request(request)
    if user_id != _ADMIN_USER_ID:
        raise HTTPException(
            status_code=403,
            detail="Restricted to admin users only.",
        )


# ── Request models ──

class CommitRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=200)


class TokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


# ── Endpoints ──

@router.get("/check-access")
async def check_access(request: Request):
    """Check if the current user has admin access to the GitHub features."""
    user_id = _get_user_id_from_request(request)
    return {"is_admin": user_id == _ADMIN_USER_ID}


@router.get("/status")
async def get_status(request: Request):
    """Return repo status: branch, remote, file status, ahead/behind info."""
    # Cache token before running git commands
    _cache_token(_get_token())

    # 0. Refresh remote refs so ahead/behind reflects what's actually on origin.
    # Without this, the cached refs are whatever was last fetched/pulled on this
    # machine, so the page reports "in sync" even when origin has new commits.
    # `--quiet` suppresses transcript noise; failures (offline, auth issue) are
    # tolerated — we fall through to whatever cached state we have.
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
async def get_log_graph(request: Request, limit: int = 80):
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
    """
    _cache_token(_get_token())

    # Refresh remote refs so origin/* branch tips reflect what's on GitHub.
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
            if existing >= 0:
                parent_lanes.append(existing)
            elif idx == 0 and active_lanes[lane] is None:
                active_lanes[lane] = p
                parent_lanes.append(lane)
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

    _cache_token(_get_token())

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


@router.post("/commit")
async def create_commit(req: CommitRequest, request: Request):
    """Stage all changes and commit with a message."""
    _require_admin(request)
    _cache_token(_get_token())

    stdout, stderr, rc = _run_git(["add", "-A"], timeout=10)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Stage failed: {stderr}")

    diff_cached, _, rc_check = _run_git(["diff", "--cached", "--quiet"], timeout=5)
    if rc_check == 0:
        return {"status": "nothing_to_commit", "message": "No changes to commit."}

    stdout, stderr, rc = _run_git(["commit", "-m", req.message], timeout=10)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Commit failed: {stderr}")

    return {
        "status": "committed",
        "message": f"Committed: {req.message}",
        "output": stdout.strip(),
    }


@router.post("/push")
async def push_to_remote(request: Request):
    """Push commits to the remote."""
    _require_admin(request)
    _cache_token(_get_token())

    stdout, stderr, rc = _run_git(["push"], timeout=30)
    if rc != 0:
        detail = stderr.strip()
        if "Authentication failed" in stderr or "could not read" in stderr:
            detail += "\n\nSet your GitHub token in the File Manager sidebar (source-control view)."
        raise HTTPException(status_code=500, detail=detail)

    return {
        "status": "pushed",
        "message": "Push successful.",
        "output": stdout.strip(),
    }


@router.post("/pull")
async def pull_from_remote(request: Request):
    """Pull from remote."""
    _require_admin(request)
    _cache_token(_get_token())

    stdout, stderr, rc = _run_git(["pull"], timeout=30)
    if rc != 0:
        detail = stderr.strip()
        if "Authentication failed" in stderr or "could not read" in stderr:
            detail += "\n\nSet your GitHub token in the File Manager sidebar (source-control view)."
        raise HTTPException(status_code=500, detail=detail)

    return {
        "status": "pulled",
        "message": "Pull successful.",
        "output": stdout.strip(),
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
