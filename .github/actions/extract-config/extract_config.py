#!/usr/bin/env python3
"""
Extract version and deployment artifact config from a uv workspace.

Discovers workspace members from the root pyproject.toml, reads each member's
pyproject.toml for version and artifact config, and compares each against the
previous commit to determine which packages need deployment.

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

    member_globs: list[str] = root_config.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])

    if not member_globs:
        # Not a workspace - the root is the only package
        return [root]

    members: list[Path] = []
    for pattern in member_globs:
        members.extend(match for match in sorted(root.glob(pattern)) if (match / "pyproject.toml").is_file())

    if not members:
        print(f"Warning: workspace member globs {member_globs} matched no packages")

    return members


def read_package_config(pyproject_path: Path) -> dict[str, Any]:
    """Read a package's pyproject.toml and return parsed config."""
    with pyproject_path.open("rb") as f:
        return tomllib.load(f)


def get_previous_version(pyproject_rel: str) -> str | None:
    """Get the version from the previous commit for a pyproject.toml path."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "show", f"HEAD~1:{pyproject_rel}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        prev_config = tomllib.loads(result.stdout)
        return prev_config["project"]["version"]
    # GitHub Actions run an older Python version that requires the brackets, hence disabling formatting
    except (subprocess.CalledProcessError, KeyError, tomllib.TOMLDecodeError):  # fmt: skip
        # File didn't exist or wasn't parseable - treat as new package
        return None


def main() -> None:
    """Extract config and write to GITHUB_OUTPUT."""
    root = Path.cwd()
    members = discover_workspace_members(root)

    all_artifacts: list[dict[str, str | list[str]]] = []
    changed_packages: list[dict[str, str]] = []

    for member_dir in members:
        pyproject_path = member_dir / "pyproject.toml"
        config = read_package_config(pyproject_path)

        name: str = config["project"]["name"]
        version: str = config["project"]["version"]
        pyproject_rel = str(pyproject_path.relative_to(root))

        # Check if version was bumped compared to previous commit
        prev_version = get_previous_version(pyproject_rel)
        if prev_version == version:
            print(f"  {name} v{version} - unchanged, skipping")
            continue

        if prev_version is None:
            print(f"  {name} v{version} - new package")
        else:
            print(f"  {name} v{version} - bumped from {prev_version}")

        changed_packages.append(
            {
                "name": name,
                "version": version,
                "path": str(member_dir.relative_to(root)),
            }
        )

        # Extract artifact definitions from [tool.bergschacht.artifacts]
        artifacts_config: dict[str, Any] = config.get("tool", {}).get("bergschacht", {}).get("artifacts", {})

        if not artifacts_config:
            print(f"  Warning: {name} has no [tool.bergschacht.artifacts] - no deployable artifacts")
            continue

        for artifact_name, artifact_def in artifacts_config.items():
            # Support both single type (string) and multiple types (list)
            raw_type: str | list[str] = artifact_def["type"]
            types: list[str] = raw_type if isinstance(raw_type, list) else [raw_type]

            for artifact_type in types:
                entry: dict[str, str | list[str]] = {
                    "name": artifact_name,
                    "type": artifact_type,
                    "version": version,
                    "package_path": str(member_dir.relative_to(root)),
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
                all_artifacts.append(entry)

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
