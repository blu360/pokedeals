"""Is a newer version available? Compares this checkout's commit with the main branch on GitHub.

Nothing changes automatically: the page shows a notice, and `python start.py update` pulls the new code and
installs new requirements (Docker users rebuild the image).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def local_commit(root: Path = ROOT) -> str | None:
    """This checkout's commit, or None when it is not a git checkout (a zip download, a Docker image)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


def check(repo: str, current: str | None = None, client: httpx.Client | None = None) -> dict:
    """{"current", "behind": commits main is ahead of us (0 = up to date), "error"}. Never raises."""
    current = current or local_commit()
    result = {"current": current, "behind": None, "repo": repo, "error": None}
    if not current:
        result["error"] = "not a git checkout"
        return result
    try:
        r = (client or httpx).get(f"https://api.github.com/repos/{repo}/compare/{current}...main", timeout=10,
                                  headers={"Accept": "application/vnd.github+json", "User-Agent": "pokedeals-update-check"})
        if r.status_code == 404:
            result["error"] = "repository not reachable (private, or this commit was never pushed)"
        elif r.is_error:
            result["error"] = f"GitHub answered {r.status_code}"
        else:
            result["behind"] = int(r.json().get("ahead_by", 0))      # main ahead of ours = updates waiting
    except (httpx.HTTPError, ValueError) as e:
        result["error"] = f"could not check ({type(e).__name__})"
    return result


def apply_update(root: Path = ROOT, say=print) -> bool:
    """git pull --ff-only, then install any changed requirements. True on success."""
    if local_commit(root) is None:
        say("This is not a git checkout, so it cannot update itself. Download the new version, or rebuild the Docker image.")
        return False
    pull = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, text=True)
    say((pull.stdout or pull.stderr).strip())
    if pull.returncode != 0:
        say("The update did not apply cleanly (you may have local changes). Nothing was changed.")
        return False
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                           "-r", str(root / "requirements.txt")])
    say("Updated. Restart pokedeals to use the new version.")
    return True
