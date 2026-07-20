#!/usr/bin/env python3
"""
Extract version and deployment artifact config from a uv workspace.

Discovers workspace members from the root pyproject.toml, reads each member's
pyproject.toml for version and artifact config, and uses the release tags on the
remote (`name/vN`) to determine which packages need deployment.

Outputs (GITHUB_OUTPUT):
  artifacts  - JSON array of artifacts whose version was bumped. Each entry:
               {name, type, version, package_path, dockerfile?, target?}
  packages   - JSON array of {name, version, path} for changed packages
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


def discover_workspace_members(root: Path) -> list[Path]:
    """
    Discover uv workspace member directories from root pyproject.toml.

    If [tool.uv.workspace.members] is defined, expand the globs.
    Otherwise treat the root itself as the only package (non-workspace repo).
    """
    with (root / "pyproject.toml").open("rb") as f:
        root_config = tomllib.load(f)

    member_globs: list[str] = (
        root_config.get("tool", {})
        .get("uv", {})
        .get("workspace", {})
        .get("members", [])
    )

    if not member_globs:
        # Not a workspace - the root is the only package
        return [root]

    members: list[Path] = []
    for pattern in member_globs:
        members.extend(
            match
            for match in sorted(root.glob(pattern))
            if (match / "pyproject.toml").is_file()
        )

    if not members:
        print(f"Warning: workspace member globs {member_globs} matched no packages")

    return members


def read_package_config(pyproject_path: Path) -> dict[str, Any]:
    """Read a package's pyproject.toml and return parsed config."""
    with pyproject_path.open("rb") as f:
        return tomllib.load(f)


def build_artifact_entries(
    artifact_name: str,
    artifact_def: dict[str, Any],
    version: str,
    member_rel: str,
) -> list[dict[str, str | list[str]]]:
    """
    Build the deploy artifact entries for one [tool.bergschacht.artifacts.<name>] block.

    A single declaration can expand to several entries when `type` is a list
    (e.g. one package published as both a docker image and a lambda).

    `member_rel` is the relative path of the package, used to populate `package_path`.
    """
    raw_type: str | list[str] = artifact_def["type"]
    types: list[str] = raw_type if isinstance(raw_type, list) else [raw_type]

    entries: list[dict[str, str | list[str]]] = []
    for artifact_type in types:
        entry: dict[str, str | list[str]] = {
            "name": artifact_name,
            "type": artifact_type,
            "version": version,
            "package_path": member_rel,
        }
        if "dockerfile" in artifact_def:
            entry["dockerfile"] = artifact_def["dockerfile"]
        # target: named multi-stage build target (docker only). Lets one
        # Dockerfile publish several images that share builder stages
        # (e.g. a Lambda and an ECS flavor of the same connector).
        if "target" in artifact_def:
            entry["target"] = artifact_def["target"]
        # extra_files: list of package-relative paths bundled into the
        # lambda zip alongside the Python install. Ignored for docker
        # artifacts (Dockerfile manages its own COPYs).
        if "extra_files" in artifact_def:
            entry["extra_files"] = artifact_def["extra_files"]
        if "extra-files" in artifact_def:
            entry["extra_files"] = artifact_def["extra-files"]
        entries.append(entry)
    return entries


def get_head_sha() -> str:
    """Return the commit sha of the checked-out HEAD."""
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_released_tags(remote: str = "origin") -> dict[str, str]:
    """
    Map each release tag on the remote to the commit it points at.

    A single `git ls-remote --tags` call is the source of truth for "which
    versions have been released".
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "ls-remote", "--tags", remote],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        # No remote / no tags - treat everything as unreleased.
        return {}

    tags: dict[str, str] = {}
    prefix = "refs/tags/"
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.startswith(prefix):
            continue
        tag = ref[len(prefix) :]
        # annotated tag
        if tag.endswith("^{}"):
            tags[tag[:-3]] = sha
        else:
            tags.setdefault(tag, sha)
    return tags


def collect_changed_artifacts(
    root: Path,
    members: list[Path],
    released_tags: dict[str, str],
    head_sha: str,
) -> tuple[list[dict[str, str | list[str]]], list[dict[str, str]]]:
    """
    Normal path: emit artifacts for every member that still needs releasing.

    A member at version N is "changed" if its release tag `name/vN` is either
    absent (a new, unreleased version) or already points at HEAD (a re-run or
    redeploy of this exact commi. A tag on any other commit means the version was already
    released elsewhere, so this is a no-op push and the member is skipped.
    """
    all_artifacts: list[dict[str, str | list[str]]] = []
    changed_packages: list[dict[str, str]] = []

    for member_dir in members:
        pyproject_path = member_dir / "pyproject.toml"
        config = read_package_config(pyproject_path)

        name: str = config["project"]["name"]
        version: str = config["project"]["version"]

        tag_commit = released_tags.get(f"{name}/v{version}")
        if tag_commit is None:
            print(f"  {name} v{version} - new release")
        elif tag_commit == head_sha:
            print(f"  {name} v{version} - tag on current commit, redeploying")
        else:
            print(
                f"  {name} v{version} - already released at {tag_commit[:8]}, skipping"
            )
            continue

        changed_packages.append(
            {
                "name": name,
                "version": version,
                "path": str(member_dir.relative_to(root)),
            }
        )

        # Extract artifact definitions from [tool.bergschacht.artifacts]
        artifacts_config: dict[str, Any] = (
            config.get("tool", {}).get("bergschacht", {}).get("artifacts", {})
        )

        if not artifacts_config:
            print(
                f"  Warning: {name} has no [tool.bergschacht.artifacts] - no deployable artifacts"
            )
            continue

        for artifact_name, artifact_def in artifacts_config.items():
            member_rel = str(member_dir.relative_to(root))
            all_artifacts.extend(
                build_artifact_entries(artifact_name, artifact_def, version, member_rel)
            )

    return all_artifacts, changed_packages


def collect_redeploy_package_version(
    root: Path,
    members: list[Path],
    redeploy_package_version: str,
) -> tuple[list[dict[str, str | list[str]]], list[dict[str, str]]]:
    """
    Redeploy path: rebuild every artifact of the package named by a release tag.

    `redeploy_package_version` is the release tag created at release time, of the form
    `<package>/v<version>` (see create-tags in deploy.yml). The package name is
    parsed from the prefix; version/config is read from the checked-out source
    (the caller checks out this tag), so all of the package's artifacts are
    rebuilt at that version without moving anything else.
    """
    # Tags are `f"{name}/v{version}"` and versions never contain "/v", so the
    # package name is everything left of the final "/v".
    package_name, sep, _ = redeploy_package_version.rpartition("/v")
    if not sep:
        print(
            f"::error::Redeploy tag '{redeploy_package_version}' is not of the form '<package>/v<version>'"
        )
        sys.exit(1)

    for member_dir in members:
        config = read_package_config(member_dir / "pyproject.toml")
        name: str = config["project"]["name"]
        if name != package_name:
            continue

        version: str = config["project"]["version"]
        member_rel = str(member_dir.relative_to(root))
        artifacts_config: dict[str, Any] = (
            config.get("tool", {}).get("bergschacht", {}).get("artifacts", {})
        )
        if not artifacts_config:
            print(
                f"::error::Redeploy package '{package_name}' has no [tool.bergschacht.artifacts] - nothing to rebuild"
            )
            sys.exit(1)

        print(
            f"  Redeploy: rebuilding all {len(artifacts_config)} artifact(s) of {name} v{version}"
        )
        entries: list[dict[str, str | list[str]]] = []
        for artifact_name, artifact_def in artifacts_config.items():
            entries.extend(
                build_artifact_entries(artifact_name, artifact_def, version, member_rel)
            )
        package = {"name": name, "version": version, "path": member_rel}
        return entries, [package]

    print(
        f"::error::Redeploy package '{package_name}' (from tag '{redeploy_package_version}') not found in any workspace member"
    )
    sys.exit(1)


def main() -> None:
    """Extract config and write to GITHUB_OUTPUT."""
    root = Path.cwd()
    members = discover_workspace_members(root)

    redeploy_package_version = os.environ.get("REDEPLOY_PACKAGE_VERSION", "").strip()
    if redeploy_package_version:
        all_artifacts, changed_packages = collect_redeploy_package_version(
            root, members, redeploy_package_version
        )
    else:
        all_artifacts, changed_packages = collect_changed_artifacts(
            root, members, get_released_tags(), get_head_sha()
        )

    docker_artifacts = [a for a in all_artifacts if a["type"] == "docker"]
    lambda_artifacts = [a for a in all_artifacts if a["type"] == "lambda"]

    print(f"\nChanged packages: {len(changed_packages)}")
    print(
        f"Artifacts to deploy: {len(all_artifacts)} (docker: {len(docker_artifacts)}, lambda: {len(lambda_artifacts)})"
    )
    print(json.dumps(all_artifacts, indent=2))

    # Write outputs
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        print("GITHUB_OUTPUT not set, printing values only")
        return

    with Path(github_output).open("a") as f:
        f.write(f"artifacts={json.dumps(all_artifacts)}\n")
        f.write(f"docker_artifacts={json.dumps(docker_artifacts)}\n")
        f.write(f"lambda_artifacts={json.dumps(lambda_artifacts)}\n")
        f.write(f"packages={json.dumps(changed_packages)}\n")
        f.write(f"has_changes={'true' if changed_packages else 'false'}\n")


if __name__ == "__main__":
    sys.exit(main())
