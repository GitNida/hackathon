#!/usr/bin/env python3
"""
Fetch Jira subtasks for a parent issue, create one branch per subtask, write
Cursor-ready context files, ask Cursor Agent to implement the work, push, and
open a GitHub pull request.

Prerequisites:
  - Jira Cloud API token: https://id.atlassian.com/manage-profile/security/api-tokens
  - GitHub fine-grained or classic token with `repo` for PRs
  - Cursor Agent CLI on PATH (default command: `agent`)
  - `git` on PATH; repo `origin` set (e.g. https://github.com/GitNida/hackathon)
  - Base branch exists locally and on remote (e.g. `main` with at least one commit)

Environment (or a `.env` file in repo root):
  JIRA_BASE_URL   e.g. https://dg-hackathon.atlassian.net
  JIRA_EMAIL      your Atlassian account email
  JIRA_API_TOKEN  your Jira API token
  GITHUB_TOKEN    GitHub PAT with repo scope
  (optional) GITHUB_OWNER  default: parsed from `git remote get-url origin`
  (optional) GITHUB_REPO   default: parsed from origin
  (optional) CURSOR_AGENT_COMMAND  default: agent
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

JIRA_API = "/rest/api/3"
ISSUE_FIELDS = "summary,description,issuetype,status,parent,subtasks"


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


def git_has_staged_changes(cwd: Path) -> bool:
    r = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode == 0:
        return False
    if r.returncode == 1:
        return True
    raise RuntimeError(f"git diff --cached --quiet failed: {r.stderr or r.stdout}")


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


def nested_name(fields: dict, key: str) -> str:
    value = fields.get(key) or {}
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return ""


def parent_key(fields: dict) -> str:
    parent = fields.get("parent") or {}
    if isinstance(parent, dict):
        return str(parent.get("key") or "")
    return ""


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


def write_task_context(
    base: Path,
    *,
    issue_key: str,
    summary: str,
    body_text: str,
    jira_base: str,
    story_key: str,
    issue_type: str,
    status: str,
    parent: str,
) -> tuple[Path, Path]:
    d = base / "tasks" / issue_key
    d.mkdir(parents=True, exist_ok=True)
    jira_url = f"{jira_base.rstrip('/')}/browse/{issue_key}"
    story_url = f"{jira_base.rstrip('/')}/browse/{story_key}" if story_key else ""
    context = d / "CONTEXT.md"
    prompt = d / "CURSOR_PROMPT.md"

    context.write_text(
        "\n".join(
            [
                f"# {issue_key}: {summary}",
                "",
                "## Jira",
                "",
                f"- Issue: [{issue_key}]({jira_url})",
                f"- Story: [{story_key}]({story_url})" if story_key else "- Story: (none)",
                f"- Parent: {parent or '(none)'}",
                f"- Type: {issue_type or '(unknown)'}",
                f"- Status: {status or '(unknown)'}",
                "",
                "## Summary",
                "",
                summary or "(none)",
                "",
                "## Description",
                "",
                body_text or "(none)",
                "",
                "## Implementation Expectations",
                "",
                "- Inspect the repository before making changes.",
                "- Implement the Jira subtask with the smallest coherent change.",
                "- Add or update focused tests when the change affects behavior.",
                "- Do not commit, push, open PRs, or modify secrets such as `.env` files.",
                "- Keep generated context files in `tasks/` available for review.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    prompt.write_text(
        "\n".join(
            [
                f"Implement Jira subtask {issue_key}: {summary}",
                "",
                f"Use the context file at `tasks/{issue_key}/CONTEXT.md` as the source of truth.",
                "Read the relevant repository files, then make the code changes needed for this task.",
                "",
                "Constraints:",
                "- Do not commit, push, or create a pull request; this script handles git and GitHub.",
                "- Do not read, print, edit, or stage `.env` files or credential files.",
                "- Keep the change scoped to the Jira task.",
                "- Add or update tests when appropriate for the implementation.",
                "- If the Jira description is insufficient, make the smallest reasonable implementation and document assumptions in the task context or code comments only when useful.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return context, prompt


def command_parts(command: str) -> list[str]:
    command = command.strip()
    if not command:
        return []
    return shlex.split(command)


def cursor_command_available(command: str) -> bool:
    parts = command_parts(command)
    if not parts:
        return False
    exe = parts[0]
    return Path(exe).exists() or shutil.which(exe) is not None


def resolve_command(parts: list[str]) -> list[str]:
    if not parts:
        return parts
    exe = parts[0]
    if Path(exe).exists():
        return parts
    resolved = shutil.which(exe)
    if resolved:
        return [resolved, *parts[1:]]
    return parts


def run_cursor_agent(
    *,
    cwd: Path,
    cursor_command: str,
    cursor_extra_args: list[str],
    prompt_path: Path,
) -> None:
    prompt_text = prompt_path.read_text(encoding="utf-8")
    parts = resolve_command(command_parts(cursor_command))
    if not parts:
        raise RuntimeError("Cursor Agent command is empty")
    cmd = [
        *parts,
        "-p",
        "--force",
        "--output-format",
        "json",
        "--workspace",
        str(cwd),
        *cursor_extra_args,
        prompt_text,
    ]
    eprint(f"Running Cursor Agent for {prompt_path.parent.name}...")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if r.stdout:
        eprint(r.stdout.strip())
    if r.stderr:
        eprint(r.stderr.strip())
    if r.returncode != 0:
        raise RuntimeError(f"Cursor Agent failed ({r.returncode})")


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
    story_key: str,
    summary_hint: str,
    dry_run: bool,
    skip_pr: bool,
    context_only: bool,
    cursor_command: str,
    cursor_extra_args: list[str],
) -> str | None:
    """Create branch, run Cursor from Jira context, push, return PR URL or None."""
    if dry_run:
        b = f"feature/{branch_slug(issue_key, summary_hint or 'summary-from-jira')}"
        eprint(f"Would use branch: {b}")
        eprint(f"Would add tasks/{issue_key}/CONTEXT.md and CURSOR_PROMPT.md")
        if context_only:
            eprint("Would skip Cursor Agent (--context-only)")
        else:
            eprint(f"Would run Cursor Agent via: {cursor_command}")
        eprint("Would commit, push to origin, and open a GitHub PR (unless --skip-pr)")
        return None

    if s is None:
        raise ValueError("Jira session required when not in dry-run")

    data = jira_get(
        s,
        jira_base,
        f"{JIRA_API}/issue/{issue_key}",
        params={"fields": ISSUE_FIELDS},
    )
    fields = data.get("fields") or {}
    summary = (fields.get("summary") or issue_key).strip()
    body_text = issue_description_text(fields)
    issue_type = nested_name(fields, "issuetype")
    status = nested_name(fields, "status")
    parent = parent_key(fields)

    branch = f"feature/{branch_slug(issue_key, summary)}"
    pr_title = f"[{issue_key}] {summary}"
    jira_browse = f"{jira_base.rstrip('/')}/browse/{issue_key}"
    pr_body = (
        f"Automated PR from Jira subtask context.\n\n"
        f"- **Jira:** [{issue_key}]({jira_browse})\n"
        f"- **Context:** `tasks/{issue_key}/CONTEXT.md`\n"
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

    current_branch = run_git_or_none(["branch", "--show-current"], cwd)
    if run_git_or_none(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd):
        if current_branch != branch:
            run_git(["checkout", branch], cwd)
        eprint(f"Branch {branch} already exists. Continuing on that branch.")
    else:
        run_git(["checkout", "-b", branch], cwd)
    _, prompt_path = write_task_context(
        cwd,
        issue_key=issue_key,
        summary=summary,
        body_text=body_text,
        jira_base=jira_base,
        story_key=story_key,
        issue_type=issue_type,
        status=status,
        parent=parent,
    )
    if context_only:
        eprint(f"Wrote Cursor context for {issue_key}; skipping Cursor Agent.")
    else:
        run_cursor_agent(
            cwd=cwd,
            cursor_command=cursor_command,
            cursor_extra_args=cursor_extra_args,
            prompt_path=prompt_path,
        )

    run_git(["add", "--all", "--", ".", ":!*.env", ":!**/.env", ":!*.pem", ":!*.key"], cwd)
    if not git_has_staged_changes(cwd):
        eprint("No staged changes after context/Cursor step; skipping commit.")
        return None
    run_git(
        [
            "commit",
            "-m",
            f"{issue_key} implement from Jira: {summary}"[:200],
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
    p = argparse.ArgumentParser(description="Jira subtasks -> Cursor Agent -> GitHub PR")
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
        "--context-only",
        action="store_true",
        help="Write Cursor context and prompt files, but do not invoke Cursor Agent.",
    )
    p.add_argument(
        "--skip-pr", action="store_true", help="Push branch but do not open a PR."
    )
    p.add_argument(
        "--cursor-command",
        default=os.environ.get("CURSOR_AGENT_COMMAND", "agent"),
        help="Cursor Agent CLI command to run (default: agent).",
    )
    p.add_argument(
        "--cursor-extra-arg",
        action="append",
        default=[],
        help="Extra argument to pass to Cursor Agent. Repeat for multiple args.",
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
        ]:
            if not val:
                eprint(f"Missing {name} in environment or .env")
                return 1
        if not args.skip_pr and not gh:
            eprint("Missing GITHUB_TOKEN in environment or .env")
            return 1
        if not args.context_only and not cursor_command_available(args.cursor_command):
            eprint(
                f"Cursor Agent command not found: {args.cursor_command}. "
                "Install the Cursor Agent CLI, set CURSOR_AGENT_COMMAND, "
                "or use --context-only."
            )
            return 1

    cwd = repo_root()
    owner, repo_name = parse_github_remote(cwd)
    owner = os.environ.get("GITHUB_OWNER", owner)
    repo_name = os.environ.get("GITHUB_REPO", repo_name)
    if not args.dry_run and not args.skip_pr and (not owner or not repo_name):
        eprint("Could not parse GITHUB_OWNER/GITHUB_REPO from `git remote origin`")
        return 1

    base_branch = args.base or default_base_branch(cwd)

    has_jira_credentials = bool(jira_base and email and jira_token)
    s = jira_session(jira_base, email, jira_token) if has_jira_credentials else None

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
            story_key=args.parent if not args.parent_only else "",
            summary_hint=item.get("summary", ""),
            dry_run=bool(args.dry_run),
            skip_pr=bool(args.skip_pr),
            context_only=bool(args.context_only),
            cursor_command=args.cursor_command,
            cursor_extra_args=args.cursor_extra_arg,
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
