"""Prune this repository's generated GHCR versions after a verified deployment."""

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode


def gh(*args):
    return subprocess.check_output(["gh", "api", *args], text=True)


def prune():
    repo = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["DEPLOYED_SHA"]
    current = json.loads(gh(f"repos/{repo}/commits/main"))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or sha != current:
        raise SystemExit("Verified deployed SHA must be current main; no images deleted.")
    owner, package = repo.split("/")
    owner_type = json.loads(gh(f"repos/{repo}"))["owner"]["type"]
    scope = "orgs" if owner_type == "Organization" else "users"
    endpoint = f"{scope}/{owner}/packages/container/{package.lower()}/versions"
    pages = json.loads(gh("--paginate", "--slurp", endpoint + "?per_page=100"))
    versions = [v for page in pages for v in page]
    keep_tag = "sha-" + sha

    def tags(version):
        return version["metadata"]["container"].get("tags", [])

    retained = [v for v in versions if keep_tag in tags(v)]
    if len(retained) != 1:
        raise SystemExit("Expected exactly one deployed version; no images deleted.")
    manifest = json.loads(subprocess.check_output([
        "docker", "buildx", "imagetools", "inspect", "--raw",
        f"ghcr.io/{repo.lower()}:{keep_tag}"
    ], text=True))
    children = {m["digest"] for m in manifest.get("manifests", [])}
    for version in versions:
        if version["id"] == retained[0]["id"] or version["name"] in children:
            continue
        if any(not re.fullmatch(r"sha-[0-9a-f]{40}", tag) for tag in tags(version)):
            continue
        if not tags(version) and children:
            continue
        print("Deleting obsolete image version", version["id"], tags(version), flush=True)
        gh("-X", "DELETE", f"{endpoint}/{version['id']}")
    print("Retained deployed image:", keep_tag)
    # GitHub log/artifact retention is native; old completed workflow run
    # records are also removed when a verified release is maintained.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    query = urlencode({"status": "completed", "created": "<" + cutoff, "per_page": 100})
    runs = json.loads(gh("--paginate", "--slurp", f"repos/{repo}/actions/runs?{query}"))
    for page in runs:
        for run in page["workflow_runs"]:
            gh("-X", "DELETE", f"repos/{repo}/actions/runs/{run['id']}")
    print("Expired completed CI records removed.")


if __name__ == "__main__":
    prune()
