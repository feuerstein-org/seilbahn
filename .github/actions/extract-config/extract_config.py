#!/usr/bin/env python3
"""
Extract package versions and deployment artifact config from `seilbahn.toml`.

`seilbahn.toml` at the repo root is the single source of truth for what this
repo builds: which packages exist, what runtime they use, and which artifacts
each one publishes.

The only language-specific read is the package *version*, which stays in the runtime's
own manifest (pyproject.toml / Cargo.toml / package.json).

Two modes, selected by SEILBAHN_MODE:

  deploy (default)
      Compare each package's version against the release tags on the remote
      (`name/vN`) and emit the artifacts of every package that still needs
      releasing.

      Outputs (GITHUB_OUTPUT):
        artifacts         - JSON array of all artifacts to build
        docker_artifacts  - the same, filtered to type=docker
        lambda_artifacts  - the same, filtered to type=lambda
        packages          - JSON array of {name, version, path} for changed packages
        has_changes       - "true" when at least one package changed

  test
      No git access and no change detection - emit every package that opts into
      the test matrix.

      Outputs (GITHUB_OUTPUT):
        test_packages     - JSON array of test ids
        has_tests         - "true" when at least one package opts in
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, NoReturn, override

# Parsed TOML. The values are `Any` deliberately: seilbahn.schema.json has already validated in CI
TomlTable = dict[str, Any]

# passed to github actions
MatrixEntry = dict[str, str | list[str]]

CONFIG_FILENAME = "seilbahn.toml"

# runtime -> (file holding the version, dotted key within it)
RUNTIME_VERSION_SOURCES: dict[str, tuple[str, str]] = {
    "python": ("pyproject.toml", "project.version"),
    "rust": ("Cargo.toml", "package.version"),
    "node": ("package.json", "version"),
}


class ConfigError(Exception):
    """A `seilbahn.toml` that cannot be acted on."""


@dataclass(frozen=True)
class Artifact:
    """
    One thing the deploy pipeline builds, an artifact a package publishes.
    """

    name: str

    type: ClassVar[str]

    def matrix_entry(self, package: Package) -> MatrixEntry:
        """Flatten this artifact and its package into one deploy-matrix entry."""
        return {
            "name": self.name,
            "type": self.type,
            "version": package.version,
            "package_path": package.path,
            "runtime": package.runtime,
            **self.build_fields(),
        }

    def build_fields(self) -> MatrixEntry:
        """The type-specific half of the matrix entry."""
        raise NotImplementedError


@dataclass(frozen=True)
class DockerArtifact(Artifact):
    """An image built from a Dockerfile inside the package."""

    type: ClassVar[str] = "docker"

    dockerfile: str
    target: str
    """Named multi-stage target; "" builds the final stage."""

    @override
    def build_fields(self) -> MatrixEntry:
        """The Dockerfile and stage `docker/build-push-action` is pointed at."""
        return {"dockerfile": self.dockerfile, "target": self.target}


@dataclass(frozen=True)
class LambdaArtifact(Artifact):
    """A zip filled by the package's own `build` command."""

    type: ClassVar[str] = "lambda"

    build: str
    output_dir: str
    extra_files: list[str]

    @override
    def build_fields(self) -> MatrixEntry:
        """The command to run, where it writes, and what else rides in the zip."""
        return {
            "build": self.build,
            "output_dir": self.output_dir,
            "extra_files": self.extra_files,
        }


@dataclass
class Package:
    """One versioned, releasable unit declared under `[packages.<name>]`."""

    name: str
    path: str
    runtime: str
    test: bool
    test_id: str
    artifacts: list[Artifact] = field(default_factory=list[Artifact])
    version: str = ""


def fail(errors: list[str]) -> NoReturn:
    """Report every collected config error as an annotation, then exit."""
    for error in errors:
        print(f"::error::{error}")
    sys.exit(1)


def dig(data: Any, dotted_key: str) -> Any:
    """Resolve a dotted key path against nested mappings, or None if absent."""
    node = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def load_data_file(path: Path) -> Any:
    """Parse a TOML or JSON file into plain Python data."""
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    return tomllib.loads(text)


def load_config(root: Path) -> TomlTable:
    """Read the root `seilbahn.toml`."""
    config_path = root / CONFIG_FILENAME
    if not config_path.is_file():
        msg = (
            f"No {CONFIG_FILENAME} at the repo root. Every seilbahn consumer must "
            f"declare its packages and artifacts there - see the seilbahn README."
        )
        raise ConfigError(msg)

    try:
        config: TomlTable = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as error:
        msg = f"{CONFIG_FILENAME} is not valid TOML: {error}"
        raise ConfigError(msg) from error

    return config


def parse_artifact(artifact_name: str, artifact_table: TomlTable) -> list[Artifact]:
    """Parses out all artifacts in one package (both docker and lambda zip)."""
    raw_type = artifact_table["type"]
    types = raw_type if isinstance(raw_type, list) else [raw_type]

    artifacts: list[Artifact] = []
    for artifact_type in types:
        if artifact_type == "docker":
            artifacts.append(
                DockerArtifact(
                    name=artifact_name,
                    dockerfile=artifact_table.get("dockerfile", "Dockerfile"),
                    # Named multi-stage target; "" builds the final stage.
                    target=artifact_table.get("target", ""),
                )
            )
        else:
            artifacts.append(
                LambdaArtifact(
                    name=artifact_name,
                    build=artifact_table["build"],
                    output_dir=artifact_table.get("output-dir", f"dist/{artifact_name}"),
                    # Always a list so the bundling step needs no "was it declared?" branch.
                    extra_files=artifact_table.get("extra-files", []),
                )
            )

    return artifacts


def parse_packages(config: TomlTable) -> list[Package]:
    """Turn `[packages.*]` tables into Package objects."""
    defaults = config.get("defaults", {})

    packages: list[Package] = []
    for name, package in config["packages"].items():
        artifacts: list[Artifact] = []
        for artifact_name, artifact_table in package.get("artifacts", {}).items():
            artifacts.extend(parse_artifact(artifact_name, artifact_table))

        packages.append(
            Package(
                name=name,
                path=package["path"],
                runtime=package.get("runtime", defaults.get("runtime")),
                test=package.get("test", True),
                test_id=package.get("test-id", name),
                artifacts=artifacts,
            )
        )

    return packages


def resolve_version(root: Path, package: Package) -> str:
    """Read a package's version from the manifest its runtime keeps it in - e.g. Cargo.toml"""
    where = f"[packages.{package.name}]"

    package_dir = root / package.path
    if not package_dir.is_dir():
        msg = f"{where}: path '{package.path}' does not exist"
        raise ConfigError(msg)

    version_file, version_key = RUNTIME_VERSION_SOURCES[package.runtime]

    version_path = package_dir / version_file
    if not version_path.is_file():
        msg = (
            f"{where}: runtime '{package.runtime}' expects the version in "
            f"{package.path}/{version_file}, which does not exist"
        )
        raise ConfigError(msg)

    try:
        data = load_data_file(version_path)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
        msg = f"{where}: cannot parse {version_file}: {error}"
        raise ConfigError(msg) from error

    version = dig(data, version_key)
    if not isinstance(version, str):
        msg = f"{where}: no string at `{version_key}` in {package.path}/{version_file}"
        raise ConfigError(msg)
    return version


def load_packages(root: Path) -> list[Package]:
    """Parse seilbahn.toml into fully resolved packages, or exit with errors."""
    try:
        config = load_config(root)
    except ConfigError as error:
        fail([str(error)])

    errors: list[str] = []
    packages = parse_packages(config)

    for package in packages:
        try:
            package.version = resolve_version(root, package)
        except ConfigError as error:
            errors.append(str(error))

    if errors:
        fail(errors)

    return packages


def get_head_sha() -> str:
    """Return the commit sha of the checked-out HEAD."""
    result = subprocess.run(
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
    packages: list[Package],
    released_tags: dict[str, str],
    head_sha: str,
) -> tuple[list[MatrixEntry], list[dict[str, str]]]:
    """
    Normal path: emit artifacts for every package that still needs releasing.

    A package at version N is "changed" if its release tag `name/vN` is either
    absent (a new, unreleased version) or already points at the commit we're running on
    (a re-run or redeploy of this exact commit). A tag on any other commit means the version
    was already released elsewhere, so this is a no-op push and the package is skipped.
    """
    all_artifacts: list[MatrixEntry] = []
    changed_packages: list[dict[str, str]] = []

    for package in packages:
        tag_commit = released_tags.get(f"{package.name}/v{package.version}")
        if tag_commit is None:
            print(f"  {package.name} v{package.version} - new release")
        elif tag_commit == head_sha:
            print(f"  {package.name} v{package.version} - tag on current commit, redeploying")
        else:
            print(f"  {package.name} v{package.version} - already released at {tag_commit[:8]}, skipping")
            continue

        changed_packages.append(
            {
                "name": package.name,
                "version": package.version,
                "path": package.path,
            }
        )

        if not package.artifacts:
            # A library: versioned and tagged, but nothing is published from it.
            print("    (no artifacts declared - tagging only)")
            continue

        all_artifacts.extend(artifact.matrix_entry(package) for artifact in package.artifacts)

    return all_artifacts, changed_packages


def collect_redeploy_package_version(
    packages: list[Package],
    redeploy_package_version: str,
) -> tuple[list[MatrixEntry], list[dict[str, str]]]:
    """Redeploy path: rebuild every artifact of the package named by a release tag."""
    # Tags are `f"{name}/v{version}"` and versions never contain "/v", so the
    # package name is everything left of the final "/v".
    package_name, sep, _ = redeploy_package_version.rpartition("/v")
    if not sep:
        fail([f"Redeploy tag '{redeploy_package_version}' is not of the form '<package>/v<version>'"])

    for package in packages:
        if package.name != package_name:
            continue

        if not package.artifacts:
            fail([f"Redeploy package '{package_name}' declares no artifacts - nothing to rebuild"])

        print(f"  Redeploy: rebuilding all {len(package.artifacts)} artifact(s) of {package.name} v{package.version}")

        return [artifact.matrix_entry(package) for artifact in package.artifacts], [
            {
                "name": package.name,
                "version": package.version,
                "path": package.path,
            }
        ]

    fail(
        [
            (
                f"Redeploy package '{package_name}' (from tag "
                f"'{redeploy_package_version}') is not declared in {CONFIG_FILENAME}"
            )
        ]
    )


def write_outputs(values: dict[str, str]) -> None:
    """Append key=value lines to GITHUB_OUTPUT, or print them when unset."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        print("GITHUB_OUTPUT not set, printing values only")
        for key, value in values.items():
            print(f"{key}={value}")
        return

    with Path(github_output).open("a") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


def run_test_mode(packages: list[Package]) -> None:
    """Emit the test matrix: every package that opts in, no git access needed."""
    test_ids = [p.test_id for p in packages if p.test]

    print(f"Packages in the test matrix: {len(test_ids)}")
    for test_id in test_ids:
        print(f"  {test_id}")

    write_outputs(
        {
            "test_packages": json.dumps(test_ids),
            "has_tests": "true" if test_ids else "false",
        }
    )


def run_deploy_mode(packages: list[Package]) -> None:
    """Emit the deploy matrices for whatever still needs releasing."""
    redeploy_package_version = os.environ.get("REDEPLOY_PACKAGE_VERSION", "").strip()
    if redeploy_package_version:
        all_artifacts, changed_packages = collect_redeploy_package_version(packages, redeploy_package_version)
    else:
        all_artifacts, changed_packages = collect_changed_artifacts(packages, get_released_tags(), get_head_sha())

    docker_artifacts = [a for a in all_artifacts if a["type"] == "docker"]
    lambda_artifacts = [a for a in all_artifacts if a["type"] == "lambda"]

    print(f"\nChanged packages: {len(changed_packages)}")
    print(
        f"Artifacts to deploy: {len(all_artifacts)} (docker: {len(docker_artifacts)}, lambda: {len(lambda_artifacts)})"
    )
    print(json.dumps(all_artifacts, indent=2))

    write_outputs(
        {
            "artifacts": json.dumps(all_artifacts),
            "docker_artifacts": json.dumps(docker_artifacts),
            "lambda_artifacts": json.dumps(lambda_artifacts),
            "packages": json.dumps(changed_packages),
            "has_changes": "true" if changed_packages else "false",
        }
    )


def main() -> None:
    """Read seilbahn.toml and write the matrices for the requested mode."""
    mode = os.environ.get("SEILBAHN_MODE", "deploy").strip() or "deploy"
    if mode not in ("deploy", "test"):
        fail([f"Unknown mode '{mode}' (expected 'deploy' or 'test')"])

    packages = load_packages(Path.cwd())
    print(f"Declared packages: {', '.join(p.name for p in packages)}\n")

    if mode == "test":
        run_test_mode(packages)
    else:
        run_deploy_mode(packages)


if __name__ == "__main__":
    sys.exit(main())
