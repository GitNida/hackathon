#!/usr/bin/env python3
"""
Fetch Jira subtasks for a parent issue, create one branch per subtask, add a small
scaffold (spec + implementation stub) from the Jira description, push, and open a
GitHub pull request.

Prerequisites:
  - Jira Cloud API token: https://id.atlassian.com/manage-profile/security/api-tokens
  - GitHub fine-grained or classic token with `repo` for PRs
  - `git` on PATH; repo `origin` set (e.g. https://github.com/GitNida/hackathon)
  - Base branch exists locally and on remote (e.g. `main` with at least one commit)

Environment (or a `.env` file in repo root):
  JIRA_BASE_URL   e.g. https://dg-hackathon.atlassian.net
  JIRA_EMAIL      your Atlassian account email
  JIRA_API_TOKEN  your Jira API token
  GITHUB_TOKEN    GitHub PAT with repo scope
  (optional) GITHUB_OWNER  default: parsed from `git remote get-url origin`
  (optional) GITHUB_REPO   default: parsed from origin
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

JIRA_API = "/rest/api/3"


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def run_git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({r.returncode}): {r.stderr or r.stdout}"
        )
    return (r.stdout or "").strip()


def run_git_or_none(args: list[str], cwd: Path) -> str | None:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def repo_root() -> Path:
    return Path(
        run_git(
            ["rev-parse", "--show-toplevel"],
            Path.cwd(),
        )
    ).resolve()


def parse_github_remote(cwd: Path) -> tuple[str, str]:
    out = run_git_or_none(["remote", "get-url", "origin"], cwd)
    if not out:
        return "", ""
    # https://github.com/Owner/Repo.git or git@github.com:Owner/Repo.git
    m = re.search(r"github\.com[:/]([^/]+)/([^/.\s]+)", out)
    if m:
        return m.group(1), re.sub(r"\.git$", "", m.group(2))
    return "", ""


def jira_session(_base_url: str, email: str, token: str) -> requests.Session:
    s = requests.Session()
    s.headers["Accept"] = "application/json"
    raw = f"{email}:{token}".encode()
    s.headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
    return s


def jira_get(
    s: requests.Session, base_url: str, path: str, params: dict | None = None
) -> Any:
    url = base_url.rstrip("/") + path
    r = s.get(url, params=params, timeout=60)
    if r.status_code != 200:
        eprint(r.text)
        r.raise_for_status()
    return r.json()


def adf_to_text(node: Any) -> str:
    """Best-effort plain text from Jira Atlassian Document Format."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)
    if "text" in node:
        return str(node.get("text", ""))
    if node.get("type") == "hardBreak":
        return "\n"
    out: list[str] = []
    for c in node.get("content", []) or []:
        t = adf_to_text(c)
        if t:
            out.append(t)
    if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
        return ("".join(out) if out else "") + "\n"
    return "".join(out)


def issue_description_text(fields: dict) -> str:
    desc = fields.get("description")
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc.strip()
    if isinstance(desc, dict) and desc.get("type") == "doc":
        return adf_to_text(desc).strip()
    return str(desc).strip()


def subtask_list(parent_json: dict) -> list[dict[str, str]]:
    """Return [{key, summary}, ...] from parent issue JSON."""
    subs = parent_json.get("fields", {}).get("subtasks", []) or []
    result: list[dict[str, str]] = []
    for st in subs:
        key = st.get("key") or ""
        sm = (st.get("fields") or {}).get("summary") or ""
        if key:
            result.append({"key": key, "summary": sm})
    return result


def branch_slug(issue_key: str, summary: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", f"{issue_key} {summary}".lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:200] if len(s) > 200 else s


def default_base_branch(cwd: Path) -> str:
    sym = run_git_or_none(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd)
    if sym:
        # refs/remotes/origin/main -> main
        if "/" in sym:
            return sym.split("/")[-1]
    for name in ("main", "master"):
        if run_git_or_none(["show-ref", "--verify", f"refs/heads/{name}"], cwd):
            return name
    return "main"


def github_request(
    method: str,
    owner: str,
    repo: str,
    path: str,
    token: str,
    body: dict | None = None,
) -> Any:
    url = f"https://api.github.com/repos/{owner}/{repo}{path}"
    h = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if method == "GET":
        r = requests.get(url, headers=h, timeout=60)
    elif method == "POST":
        r = requests.post(url, headers=h, json=body, timeout=60)
    else:
        raise ValueError(method)
    if r.status_code not in (200, 201):
        eprint(r.text)
        r.raise_for_status()
    if r.text:
        return r.json()
    return {}


def write_task_scaffold(
    base: Path, issue_key: str, summary: str, body_text: str, jira_base: str
) -> None:
    d = base / "tasks" / issue_key
    d.mkdir(parents=True, exist_ok=True)
    spec = d / "SPEC.txt"
    jira_url = f"{jira_base.rstrip('/')}/browse/{issue_key}"
    spec.write_text(
        f"Issue: {issue_key}\nJira: {jira_url}\n\nSummary:\n{summary}\n\nDescription:\n{body_text or '(none)'}\n",
        encoding="utf-8",
    )
    impl = d / "implementation.py"
    if not impl.exists():
        impl.write_text(
            f'"""\nImplement SCRUM work for {issue_key} — see tasks/{issue_key}/SPEC.txt\n"""\n\n# TODO: implement per Jira description\n\ndef run() -> None:\n    raise NotImplementedError("{issue_key}")\n',
            encoding="utf-8",
        )


def process_one_issue(
    *,
    cwd: Path,
    s: Optional[requests.Session],
    jira_base: str,
    issue_key: str,
    base_branch: str,
    owner: str,
    repo: str,
    github_token: str,
    dry_run: bool,
    skip_pr: bool,
) -> str | None:
    """Create branch, commit scaffold, push, return PR URL or None."""
    if dry_run:
        b = f"feature/{branch_slug(issue_key, 'summary-from-jira')}"
        eprint(f"Would use branch: {b}")
        eprint(f"Would add tasks/{issue_key}/ (SPEC + implementation.py)")
        eprint("Would push to origin and open a GitHub PR (unless --skip-pr)")
        return None

    if s is None:
        raise ValueError("Jira session required when not in dry-run")

    data = jira_get(
        s,
        jira_base,
        f"{JIRA_API}/issue/{issue_key}",
        params={"fields": "summary,description,issuetype,status,subtasks"},
    )
    fields = data.get("fields") or {}
    summary = (fields.get("summary") or issue_key).strip()
    body_text = issue_description_text(fields)

    branch = f"feature/{branch_slug(issue_key, summary)}"
    pr_title = f"[{issue_key}] {summary}"
    jira_browse = f"{jira_base.rstrip('/')}/browse/{issue_key}"
    pr_body = (
        f"Automated PR from Jira subtask.\n\n"
        f"- **Jira:** [{issue_key}]({jira_browse})\n"
    )

    if run_git_or_none(["rev-parse", f"refs/heads/{base_branch}"], cwd) is None:
        try:
            run_git(["show-ref", "--verify", f"refs/remotes/origin/{base_branch}"], cwd)
            run_git(["checkout", "-B", base_branch, f"origin/{base_branch}"], cwd)
        except RuntimeError as ex:
            eprint(
                f"Base branch {base_branch} is missing locally and on origin. {ex}\n"
                f"Create {base_branch} and push, or set --base."
            )
            raise

    run_git(["fetch", "origin", base_branch], cwd)
    run_git(["checkout", base_branch], cwd)
    run_git(["pull", "origin", base_branch], cwd)

    if run_git_or_none(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd):
        eprint(
            f"Branch {branch} already exists. Delete it or use a new issue. Skipping."
        )
        return None

    run_git(["checkout", "-b", branch], cwd)
    write_task_scaffold(cwd, issue_key, summary, body_text, jira_base)
    run_git(["add", f"tasks/{issue_key}"], cwd)
    status = run_git(["status", "--porcelain"], cwd)
    if not status:
        eprint("Nothing to commit; skipping")
        return None
    run_git(
        [
            "commit",
            "-m",
            f"{issue_key} scaffold from Jira: {summary}"[:200],
        ],
        cwd
    )
    run_git(["push", "-u", "origin", branch], cwd)

    if skip_pr:
        eprint(f"Pushed {branch} (--skip-pr). Open PR manually.")
        return None

    r = github_request(
        "POST",
        owner,
        repo,
        "/pulls",
        github_token,
        body={
            "title": pr_title[:200],
            "head": branch,
            "base": base_branch,
            "body": pr_body,
        },
    )
    return str(r.get("html_url", "")) or None


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Jira subtasks -> git branch -> GitHub PR")
    p.add_argument(
        "--parent",
        default="SCRUM-10",
        help="Parent Jira key whose subtasks to process (default SCRUM-10).",
    )
    p.add_argument(
        "--base",
        default="",
        help="Git base branch (default: detect main or origin/HEAD).",
    )
    p.add_argument(
        "--parent-only",
        action="store_true",
        help="Ignore subtasks; create one branch+PR for the --parent issue itself.",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Print actions without git/GitHub."
    )
    p.add_argument(
        "--skip-pr", action="store_true", help="Push branch but do not open a PR."
    )
    p.add_argument(
        "--max",
        type=int,
        default=0,
        help="Max number of subtasks to process (0 = all).",
    )
    args = p.parse_args()

    jira_base = (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    gh = os.environ.get("GITHUB_TOKEN", "")

    if not args.dry_run:
        for name, val in [
            ("JIRA_BASE_URL", jira_base),
            ("JIRA_EMAIL", email),
            ("JIRA_API_TOKEN", jira_token),
            ("GITHUB_TOKEN", gh),
        ]:
            if not val:
                eprint(f"Missing {name} in environment or .env")
                return 1

    cwd = repo_root()
    owner, repo_name = parse_github_remote(cwd)
    owner = os.environ.get("GITHUB_OWNER", owner)
    repo_name = os.environ.get("GITHUB_REPO", repo_name)
    if not args.dry_run and (not owner or not repo_name):
        eprint("Could not parse GITHUB_OWNER/GITHUB_REPO from `git remote origin`")
        return 1

    base_branch = args.base or default_base_branch(cwd)

    s = jira_session(jira_base, email, jira_token) if not args.dry_run else None

    issue_keys: list[dict[str, str]] = []
    if args.parent_only:
        issue_keys = [
            {
                "key": args.parent,
                "summary": args.parent,
            }
        ]  # summary updated after fetch
    else:
        if s is None:
            eprint("Dry-run with subtasks: would fetch parent and list subtasks")
            return 0
        parent = jira_get(
            s,
            jira_base,
            f"{JIRA_API}/issue/{args.parent}",
            params={"fields": "subtasks,summary"},
        )
        st = subtask_list(parent)
        if not st:
            eprint(
                f"No subtasks on {args.parent}. Use --parent-only to branch that issue, "
                "or add subtasks in Jira."
            )
            return 2
        for item in st:
            issue_keys.append(
                {
                    "key": item["key"],
                    "summary": item.get("summary", item["key"]),
                }
            )

    if args.max and len(issue_keys) > args.max:
        issue_keys = issue_keys[: args.max]

    if args.dry_run and not args.parent_only:
        eprint(
            f"[dry-run] Would list subtasks of {args.parent} and for each: branch, tasks/<KEY>/, push, PR"
        )
        return 0

    urls: list[str] = []
    for i, item in enumerate(issue_keys):
        key = item["key"]
        eprint(f"--- ({i + 1}/{len(issue_keys)}) {key} ---")
        u = process_one_issue(
            cwd=cwd,
            s=s,
            jira_base=jira_base,
            issue_key=key,
            base_branch=base_branch,
            owner=owner,
            repo=repo_name,
            github_token=gh,
            dry_run=bool(args.dry_run),
            skip_pr=bool(args.skip_pr),
        )
        if u:
            urls.append(u)
            eprint("PR:", u)

    if urls:
        print("Opened PRs:")
        for u in urls:
            print(" ", u)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (requests.HTTPError, RuntimeError, OSError) as e:
        eprint(e)
        raise SystemExit(1) from e
