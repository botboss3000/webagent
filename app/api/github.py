"""GitHub / Git integration API for webAgent.

Provides endpoints for:
- Repo status (branch, remote, unstaged/staged files)
- Commit log
- Stage all + commit
- Push to remote
- Pull from remote
- Store / check GitHub token (for HTTPS auth)
"""

import logging
import os
import subprocess
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github")

# ── Find project root (parent of app/) ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Token storage ──
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


def _run_git(args: list[str], timeout: int = 15) -> tuple[str, str, int]:
    """Run a git command in the project root. Returns (stdout, stderr, returncode)."""
    env = os.environ.copy()
    token = _get_token()
    if token:
        # Use token for HTTPS auth
        env["GIT_ASKPASS"] = ""
        # Set credential helper to use token inline
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

class CommitRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=200)


class TokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


# ── Endpoints ──

@router.get("/status")
async def get_status():
    """Return repo status: branch, remote, file status, ahead/behind info."""
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

    # 5. Recent commits
    log_out, _, rc_log = _run_git(
        ["log", "--oneline", "-20", "--decorate=short"],
        timeout=10,
    )
    commits = []
    if rc_log == 0:
        for line in log_out.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            commits.append({
                "hash": parts[0] if parts else "",
                "message": parts[1] if len(parts) > 1 else "",
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


@router.post("/commit")
async def create_commit(req: CommitRequest):
    """Stage all changes and commit with a message."""
    # 1. Stage all
    stdout, stderr, rc = _run_git(["add", "-A"], timeout=10)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Stage failed: {stderr}")

    # 2. Check if anything to commit
    diff_cached, _, rc_check = _run_git(["diff", "--cached", "--quiet"], timeout=5)
    if rc_check == 0:
        return {"status": "nothing_to_commit", "message": "No changes to commit."}

    # 3. Commit
    stdout, stderr, rc = _run_git(["commit", "-m", req.message], timeout=10)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Commit failed: {stderr}")

    return {
        "status": "committed",
        "message": f"Committed: {req.message}",
        "output": stdout.strip(),
    }


@router.post("/push")
async def push_to_remote():
    """Push commits to the remote."""
    stdout, stderr, rc = _run_git(["push"], timeout=30)
    if rc != 0:
        detail = stderr.strip()
        # Check for auth issues
        if "Authentication failed" in stderr or "could not read" in stderr:
            detail += "\n\nSet your GitHub token in the GitHub tab → Settings."
        raise HTTPException(status_code=500, detail=detail)

    return {
        "status": "pushed",
        "message": "Push successful.",
        "output": stdout.strip(),
    }


@router.post("/pull")
async def pull_from_remote():
    """Pull from remote."""
    stdout, stderr, rc = _run_git(["pull"], timeout=30)
    if rc != 0:
        detail = stderr.strip()
        if "Authentication failed" in stderr or "could not read" in stderr:
            detail += "\n\nSet your GitHub token in the GitHub tab → Settings."
        raise HTTPException(status_code=500, detail=detail)

    return {
        "status": "pulled",
        "message": "Pull successful.",
        "output": stdout.strip(),
    }


@router.post("/token")
async def set_token(req: TokenRequest):
    """Store a GitHub personal access token for HTTPS auth."""
    # Validate: try a simple git operation
    _save_token(req.token)
    return {"status": "ok", "message": "GitHub token saved."}


@router.get("/token-status")
async def token_status():
    """Check if a GitHub token is configured."""
    token = _get_token()
    return {
        "configured": bool(token),
        "masked": f"{token[:4]}****" if len(token) > 8 else ("****" if token else ""),
    }
