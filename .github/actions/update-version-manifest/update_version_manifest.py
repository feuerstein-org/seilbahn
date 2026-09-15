#!/usr/bin/env python3
"""
Update the committed version manifest in the CDK repo.

Reads deploy-result JSON files and commits the new artifact metadata to
`version-manifests/latest.json` in the CDK repo via the GitHub contents API
(no checkout, optimistic concurrency with retry on conflicting writes).
This script runs inside the application repo's deploy workflow.

Manifest entry shape (under repositories.{repo}.packages.{package}.{images|lambdas}.{name}):
  {
    "latest": {...},   # newest published artifact - CDK uses this unless pinned
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifact_identity import MANIFEST_SCHEMA_VERSION, lambda_key, release_id, validate_name, validate_version

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
    """Parse a canonical MAJOR.MINOR.PATCH version into a comparable tuple."""
    validate_version(version)
    return tuple(int(p) for p in version.split("."))


def is_newer_version(new_version: str, current_version: str | None) -> bool:
    """Check if new_version is newer than current_version."""
    new_parsed = parse_version(new_version)
    if current_version is None:
        return True

    return new_parsed > parse_version(current_version)


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


def validate_manifest_source(manifest: JsonObject, repo_name: str, source_repo: str) -> None:
    """Check the manifest contract and ownership before recording any new results."""
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        msg = f"Expected manifest schemaVersion {MANIFEST_SCHEMA_VERSION}"
        raise ValueError(msg)
    validate_name(repo_name, "Repository name")
    owner, separator, source_name = source_repo.partition("/")
    if not owner or not separator or source_name != repo_name:
        msg = f"Source repository '{source_repo}' does not match repository key '{repo_name}'"
        raise ValueError(msg)
    repositories = manifest["repositories"]
    if repo_name in repositories:
        existing_source = repositories[repo_name]["source"]
        if existing_source.casefold() != source_repo.casefold():
            msg = f"Repository key '{repo_name}' already belongs to '{existing_source}', not '{source_repo}'"
            raise ValueError(msg)


def validate_updates(manifest: JsonObject, repo_name: str, source_repo: str, artifacts: list[Any]) -> None:
    """Reject ambiguous batches or incompatible manifests before mutating anything."""
    validate_manifest_source(manifest, repo_name, source_repo)
    seen: set[tuple[str, str, str]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or any(not isinstance(v, str) or not v for v in artifact.values()):
            msg = "Deploy results must be objects containing non-empty strings"
            raise ValueError(msg)
        fields = {
            "image": {"repository", "imageTag"},
            "lambda": {"bucket", "key", "objectVersion"},
        }.get(artifact.get("type", ""))
        if fields is None:
            msg = f"Unknown artifact type '{artifact.get('type')}'"
            raise ValueError(msg)
        fields |= {"package", "name", "type", "version"}
        if artifact.keys() != fields:
            msg = f"A {artifact['type']} deploy result requires exactly these fields: {', '.join(sorted(fields))}"
            raise ValueError(msg)
        if artifact.get("objectVersion") == "null":
            msg = "Lambda objectVersion must not be 'null'"
            raise ValueError(msg)
        package = artifact["package"]
        name = artifact["name"]
        identity = (package, artifact["type"], name)
        if identity in seen:
            msg = f"Duplicate deploy result for {repo_name}/{package}/{artifact['type']}/{name}"
            raise ValueError(msg)
        seen.add(identity)
        expected_tag = release_id(repo_name, package, name, artifact["version"])
        if artifact["type"] == "image":
            if artifact["imageTag"] != expected_tag:
                msg = f"Image tag for {repo_name}/{package}/{name} must be '{expected_tag}'"
                raise ValueError(msg)
        else:
            expected_key = lambda_key(repo_name, package, name, artifact["version"])
            if artifact["key"] != expected_key:
                msg = f"Lambda key for {repo_name}/{package}/{name} must be '{expected_key}'"
                raise ValueError(msg)


def apply_updates(
    manifest: JsonObject,
    repo_name: str,
    source_repo: str,
    commit_sha: str,
    artifacts: list[JsonObject],
) -> tuple[bool, bool, list[str]]:
    """
    Record published artifacts in the version manifest, mutating it in place.

    For each artifact, the matching manifest entry's `latest` pointer is moved to
    the new published version, but only when that version is strictly newer than the
    version already recorded (see `is_newer_version`); older or equal versions are
    skipped and leave the manifest untouched. New repositories, packages and artifacts
    are registered as needed. Existing entries must have the required fields from
    the current manifest schema.

    Artifacts are already available in ECR/S3 before this function runs. `latest`
    records the newest published build even while pinned. CDK resolves `pinned`
    before `latest`, so advancing a pinned entry does not change the artifact
    selected by this manifest. Removing the pin selects the recorded `latest`.

    Args:
        manifest: The manifest dict to update in place.
        repo_name: Key identifying this repo's entry within `repositories`.
        source_repo: Source repository, stored when registering a new repository.
        commit_sha: Commit the artifacts were built from, embedded in each payload.
        artifacts: Deploy results to apply; each is normalized via `build_artifact`.

    Returns:
        A tuple `(resolved_changed, manifest_changed, updated_labels)`:
          - resolved_changed: True if at least one unpinned entry advanced, changing
            what this manifest selects for CDK. This does not report publication
            or deployment success. Pinned advances do not set this.
          - manifest_changed: True if any `latest` pointer advanced (also happens if pinned),
            i.e. new manifest needs to be committed.
          - updated_labels: `"{package}/{kind}/{name}@{version}"` strings for the unpinned entries
            that advanced, suitable for logging or a commit/PR summary. Pinned
            advances and skipped artifacts are excluded.

    """
    validate_updates(manifest, repo_name, source_repo, artifacts)
    repo_entry = manifest["repositories"].setdefault(repo_name, {"source": source_repo, "packages": {}})

    resolved_changed = False
    manifest_changed = False
    updated_labels: list[str] = []

    for artifact in artifacts:
        package = artifact["package"]
        name = artifact["name"]
        kind, payload = build_artifact(artifact, commit_sha)
        version = payload["version"]

        package_entry = repo_entry["packages"].setdefault(package, {})
        entries = package_entry.setdefault(kind, {})
        entry = entries.get(name)
        current = entry["latest"]["version"] if entry is not None else None

        if not is_newer_version(version, current):
            print(
                f"Skipping manifest update for {repo_name}/{package}/{kind}/{name}: published version {version} "
                f"is not newer than manifest latest {current}"
            )
            continue

        if entry is None:
            entry = entries[name] = {}
        entry["latest"] = payload
        manifest_changed = True
        if entry.get("pinned"):
            print(
                f"{repo_name}/{package}/{kind}/{name}: recording published version {version} as latest; "
                f"this manifest still selects pinned version {entry['pinned']['version']}"
            )
        else:
            resolved_changed = True
            updated_labels.append(f"{package}/{kind}/{name}@{version}")
            print(f"{repo_name}/{package}/{kind}/{name}: this manifest now selects published version {version}")

    return resolved_changed, manifest_changed, updated_labels


def commit_manifest(env: dict[str, str], artifacts: list[JsonObject]) -> tuple[bool, bool]:
    """
    Read the manifest, apply the deploy results, and commit it back.

    The read-modify-write is optimistic, a conflicting write from another repo's
    deploy is retried from a fresh read.

    Returns `(ok, resolved_changed)`.
    """
    contents_url = f"{API_ROOT}/repos/{env['MANIFEST_REPO']}/contents/{env['MANIFEST_PATH']}"
    token = env["GITHUB_TOKEN"]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        current = github_request(f"{contents_url}?ref={env['MANIFEST_BRANCH']}", token)
        manifest = json.loads(base64.b64decode(current["content"]))

        resolved_changed, manifest_changed, updated_labels = apply_updates(
            manifest, env["REPO_NAME"], env["SOURCE_REPO"], env["COMMIT_SHA"], artifacts
        )

        if not manifest_changed:
            print("No manifest changes (all versions are current or older)")
            return True, resolved_changed

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
            return False, resolved_changed

        print(f"Committed manifest update: {result['commit']['sha']}")
        return True, resolved_changed

    print(f"Failed to commit manifest update after {MAX_ATTEMPTS} attempts")
    return False, False


def main() -> int:
    """Commit deploy results to the CDK repo's version manifest."""
    parser = argparse.ArgumentParser(description="Update the committed version manifest after artifact publication")
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

    try:
        artifacts: list[JsonObject] = [json.loads(f.read_text()) for f in result_files]
        ok, resolved_changed = commit_manifest(env, artifacts)
    except ValueError as error:
        print(f"Error: {error}")
        return 1
    if not ok:
        return 1

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a") as f:
            f.write(f"updated={'true' if resolved_changed else 'false'}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
