#!/usr/bin/env python3
"""
Update the committed version manifest in the CDK repo.

Reads deploy-result JSON files and commits the new artifact metadata to
`version-manifests/latest.json` in the CDK repo via the GitHub contents API
(no checkout, optimistic concurrency with retry on conflicting writes).
This script runs inside the application repo's deploy workflow.

Manifest entry shape (per artifact, under repositories.{repo}.{images|lambdas}.{name}):
  {
    "latest": {...},   # newest published artifact - what CDK synth resolves
    "pinned": {...}    # optional; deploys instead of latest when present
  }

This script only ever advances `latest` (and only forward). It never touches
`pinned` - pins are created and lifted in the CDK repo (`mise run pin|unpin`).

Environment variables:
  REPO_NAME        - The repository name (e.g. sample-python-repo)
  COMMIT_SHA       - The git commit SHA of the app repo release
  SOURCE_REPO      - The app repository in owner/repo form
  MANIFEST_REPO    - The CDK repository in owner/repo form
  MANIFEST_PATH    - Path of the manifest file in the CDK repo
  MANIFEST_BRANCH  - Branch to commit to (e.g. master)
  GITHUB_TOKEN     - GitHub App installation token with contents:write on MANIFEST_REPO

Inputs:
  --results-dir    - Path to directory containing deploy result JSON files
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

REQUIRED_ENV = (
    "REPO_NAME",
    "COMMIT_SHA",
    "SOURCE_REPO",
    "MANIFEST_REPO",
    "MANIFEST_PATH",
    "MANIFEST_BRANCH",
    "GITHUB_TOKEN",
)

MAX_ATTEMPTS = 5
API_ROOT = "https://api.github.com"

# Another writer committed to the manifest between our read and our write.
HTTP_CONFLICT = 409


def parse_version(version: str) -> tuple[int, ...]:
    """Parse a semantic version string into comparable tuple."""
    version = version.lstrip("v")
    return tuple(int(p) for p in version.split("."))


def is_newer_version(new_version: str, current_version: str | None) -> bool:
    """Check if new_version is newer than current_version."""
    if current_version is None:
        return True

    try:
        new_parsed = parse_version(new_version)
        current_parsed = parse_version(current_version)
    except ValueError:
        print(f"Warning: Could not parse versions ({new_version}, {current_version}), treating as older")
        return False
    else:
        return new_parsed > current_parsed


def github_request(url: str, token: str, method: str = "GET", body: JsonObject | None = None) -> Any:
    """Perform a GitHub API request, returning the parsed JSON response."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 API_ROOT guarantees HTTPS
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        return json.load(response)


def build_artifact(artifact: JsonObject, commit_sha: str) -> tuple[str, JsonObject]:
    """Map a deploy result to its manifest kind and artifact payload."""
    a_type = artifact["type"]
    if a_type == "image":
        return "images", {
            "repository": artifact["repository"],
            "imageTag": artifact["imageTag"],
            "version": artifact["version"],
            "commitSha": commit_sha,
        }
    if a_type == "lambda":
        return "lambdas", {
            "bucket": artifact["bucket"],
            "key": artifact["key"],
            "objectVersion": artifact["objectVersion"],
            "version": artifact["version"],
            "commitSha": commit_sha,
        }
    msg = f"Unknown artifact type '{a_type}' for artifact '{artifact['name']}'"
    raise ValueError(msg)


def apply_updates(
    manifest: JsonObject,
    repo_name: str,
    source_repo: str,
    commit_sha: str,
    artifacts: list[JsonObject],
) -> tuple[bool, bool, list[str]]:
    """
    Record freshly deployed artifacts in the version manifest, mutating it in place.

    For each artifact, the matching manifest entry's `latest` pointer is moved to
    the new deployed version, but only when that version is strictly newer than the
    version already recorded (see `is_newer_version`); older or equal versions are
    skipped and leave the manifest untouched. Missing `repositories`, repo, kind,
    and entry containers are created as needed, and the repo entry's `source` is
    backfilled from `source_repo` if absent.

    A `latest` pointer is purely a record of the newest known build. Whether that
    build actually deploys depends on `pinned`: if an entry is pinned, `latest`
    still advances but the pinned version is what deploys, so `latest` can move
    forward without changing anything that runs.

    Args:
        manifest: The manifest dict to update in place.
        repo_name: Key identifying this repo's entry within `repositories`.
        source_repo: Source repository, stored as the entry's `source` if not set.
        commit_sha: Commit the artifacts were built from, embedded in each payload.
        artifacts: Deploy results to apply; each is normalized via `build_artifact`.

    Returns:
        A tuple `(deploy_changed, manifest_changed, updated_labels)`:
          - deploy_changed: True if at least one unpinned entry advanced, i.e. what
            actually deploys moved. Pinned advances do not set this.
          - manifest_changed: True if any `latest` pointer advanced (also happens if pinned),
            i.e. new manifest needs to be committed.
          - updated_labels: `"{name}@{version}"` strings for the unpinned entries
            that advanced, suitable for logging or a commit/PR summary. Pinned
            advances and skipped artifacts are excluded.

    """
    repo_entry = manifest.setdefault("repositories", {}).setdefault(repo_name, {})
    repo_entry.setdefault("source", source_repo)

    deploy_changed = False
    manifest_changed = False
    updated_labels: list[str] = []

    for artifact in artifacts:
        name = artifact["name"]
        kind, payload = build_artifact(artifact, commit_sha)
        version = payload["version"]

        entry = repo_entry.setdefault(kind, {}).setdefault(name, {})
        current = entry.get("latest", {}).get("version")

        if not is_newer_version(version, current):
            print(
                f"Skipping {repo_name}/{kind}/{name}: deployed version {version} "
                f"is not newer than manifest latest {current}"
            )
            continue

        entry["latest"] = payload
        manifest_changed = True
        if entry.get("pinned"):
            print(
                f"{repo_name}/{kind}/{name} is pinned at {entry['pinned'].get('version')}: "
                f"advancing latest to {version} without deploying it"
            )
        else:
            deploy_changed = True
            updated_labels.append(f"{name}@{version}")
            print(f"Updated {repo_name}/{kind}/{name} to {version}")

    return deploy_changed, manifest_changed, updated_labels


def commit_manifest(env: dict[str, str], artifacts: list[JsonObject]) -> tuple[bool, bool]:
    """
    Read the manifest, apply the deploy results, and commit it back.

    The read-modify-write is optimistic, a conflicting write from another repo's
    deploy is retried from a fresh read.

    Returns `(ok, deploy_changed)`.
    """
    contents_url = f"{API_ROOT}/repos/{env['MANIFEST_REPO']}/contents/{env['MANIFEST_PATH']}"
    token = env["GITHUB_TOKEN"]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        current = github_request(f"{contents_url}?ref={env['MANIFEST_BRANCH']}", token)
        manifest = json.loads(base64.b64decode(current["content"]))

        deploy_changed, manifest_changed, updated_labels = apply_updates(
            manifest, env["REPO_NAME"], env["SOURCE_REPO"], env["COMMIT_SHA"], artifacts
        )

        if not manifest_changed:
            print("No manifest changes (all versions are current or older)")
            return True, deploy_changed

        message = (
            f"chore: update {env['REPO_NAME']} artifacts ({', '.join(updated_labels)})"
            if updated_labels
            else f"chore: track latest {env['REPO_NAME']} artifacts (pinned)"
        )
        message += f"\n\nSource: {env['SOURCE_REPO']}@{env['COMMIT_SHA']}"

        body: JsonObject = {
            "message": message,
            "content": base64.b64encode((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()).decode(),
            "sha": current["sha"],
            "branch": env["MANIFEST_BRANCH"],
        }

        try:
            result = github_request(contents_url, token, method="PUT", body=body)
        except urllib.error.HTTPError as error:
            if error.code == HTTP_CONFLICT and attempt < MAX_ATTEMPTS:
                print(f"Conflicting manifest write (attempt {attempt}/{MAX_ATTEMPTS}), retrying...")
                time.sleep(attempt * 2)
                continue
            print(f"Error: manifest update failed with HTTP {error.code}: {error.read().decode()}")
            return False, deploy_changed

        print(f"Committed manifest update: {result['commit']['sha']}")
        return True, deploy_changed

    print(f"Failed to commit manifest update after {MAX_ATTEMPTS} attempts")
    return False, False


def main() -> int:
    """Commit deploy results to the CDK repo's version manifest."""
    parser = argparse.ArgumentParser(description="Update the committed version manifest after artifact deployment")
    parser.add_argument("--results-dir", required=True, help="Directory containing result JSON files")
    args = parser.parse_args()

    env: dict[str, str] = {}
    missing: list[str] = []
    for name in REQUIRED_ENV:
        value = os.environ.get(name)
        if value:
            env[name] = value
        else:
            missing.append(name)

    if missing:
        print(f"Error: missing required environment variables: {', '.join(missing)}")
        return 1

    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(f"Error: Results directory not found: {results_dir}")
        return 1

    result_files = sorted(results_dir.glob("*.json"))
    if not result_files:
        print(f"Error: No deploy result JSON files found in {results_dir}")
        return 1

    artifacts: list[JsonObject] = [json.loads(f.read_text()) for f in result_files]

    ok, deploy_changed = commit_manifest(env, artifacts)
    if not ok:
        return 1

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a") as f:
            f.write(f"updated={'true' if deploy_changed else 'false'}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
