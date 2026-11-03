#!/usr/bin/env python3
"""Push the current local Git commit to GitHub through the REST API.

Some restricted networks allow api.github.com but block github.com:443, which
makes `git push` impossible.  This script rebuilds the commit server-side with
the Git Data API (blobs -> tree -> commit -> ref), preserving paths, modes and
file contents.

Usage:
    GH_TOKEN=<token> python3 tools/push_api.py [--repo OGpig/RK3576] [--branch main]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"


def git(*args: str, binary: bool = False):
    output = subprocess.check_output(["git", *args])
    return output if binary else output.decode("utf-8", "replace").strip()


def api(method: str, path: str, payload: dict | None = None, token: str = "") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        API_ROOT + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "rkdesk-push-api",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise SystemExit(f"API {method} {path} failed: {error.code}\n{body}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="OGpig/RK3576")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise SystemExit("GH_TOKEN is required")

    head = git("rev-parse", "HEAD")
    subject = git("log", "-1", "--format=%s")
    body = git("log", "-1", "--format=%b")
    message = args.message or (subject + ("\n\n" + body if body else ""))
    author_name = git("log", "-1", "--format=%an")
    author_email = git("log", "-1", "--format=%ae")
    author_date = git("log", "-1", "--format=%aI")
    committer_name = git("log", "-1", "--format=%cn")
    committer_email = git("log", "-1", "--format=%ce")
    committer_date = git("log", "-1", "--format=%cI")

    print(f"[api] repo={args.repo} branch={args.branch} local={head[:8]}")
    print(f"[api] commit: {subject}")

    entries = []
    tree_listing = git("ls-tree", "-r", "HEAD")
    for line in tree_listing.splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob":
            continue
        content = git("cat-file", "blob", sha, binary=True)
        encoded = base64.b64encode(content).decode()
        blob = api("POST", f"/repos/{args.repo}/git/blobs",
                   {"content": encoded, "encoding": "base64"}, token)
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        print(f"  blob {path}  {len(content)} B")

    tree = api("POST", f"/repos/{args.repo}/git/trees", {"tree": entries}, token)
    print(f"[api] tree {tree['sha'][:8]} ({len(entries)} files)")

    parents = []
    try:
        reference = api("GET", f"/repos/{args.repo}/git/ref/heads/{args.branch}", token=token)
        parents = [reference["object"]["sha"]]
        print(f"[api] remote head {parents[0][:8]}")
    except SystemExit as error:
        if "404" not in str(error):
            raise
        print("[api] remote branch does not exist yet")

    commit = api("POST", f"/repos/{args.repo}/git/commits", {
        "message": message,
        "tree": tree["sha"],
        "parents": parents,
        "author": {"name": author_name, "email": author_email, "date": author_date},
        "committer": {"name": committer_name, "email": committer_email, "date": committer_date},
    }, token)
    print(f"[api] commit {commit['sha'][:8]}")

    if parents:
        api("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.branch}",
            {"sha": commit["sha"], "force": False}, token)
    else:
        api("POST", f"/repos/{args.repo}/git/refs",
            {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]}, token)
    print(f"[api] refs/heads/{args.branch} -> {commit['sha'][:8]}")

    check = api("GET", f"/repos/{args.repo}/git/commits/{commit['sha']}", token=token)
    print(f"[api] verified tree {check['tree']['sha'][:8]}  url={check.get('html_url', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
