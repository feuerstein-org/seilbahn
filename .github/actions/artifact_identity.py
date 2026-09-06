"""Canonical artifact identities shared by config extraction and manifest updates."""

import re

MANIFEST_SCHEMA_VERSION = "2.0.0"
MAX_RELEASE_ID_LENGTH = 128
NAME_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*", re.ASCII)
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", re.ASCII)


def validate_name(name: str, label: str) -> None:
    """Reserve dots for separators and reject names requiring tag sanitization."""
    if not NAME_PATTERN.fullmatch(name):
        msg = f"{label} '{name}' must match {NAME_PATTERN.pattern}; dots are reserved for artifact separators"
        raise ValueError(msg)


def validate_version(version: str) -> None:
    """Require the same canonical release version before publishing and recording it."""
    if not VERSION_PATTERN.fullmatch(version):
        msg = f"Version '{version}' must be MAJOR.MINOR.PATCH with no leading zeros, prefix, or prerelease suffix"
        raise ValueError(msg)


def release_id(repo: str, package: str, artifact: str, version: str) -> str:
    """Build an unambiguous release identifier valid as a Docker tag."""
    for name, label in ((repo, "Repository name"), (package, "Package name"), (artifact, "Artifact name")):
        validate_name(name, label)
    validate_version(version)
    identifier = f"{repo}.{package}.{artifact}.{version}"
    if len(identifier) > MAX_RELEASE_ID_LENGTH:
        msg = f"Artifact release identifier '{identifier}' is {len(identifier)} characters; maximum is 128"
        raise ValueError(msg)
    return identifier


def lambda_key(repo: str, package: str, artifact: str, version: str) -> str:
    """Preserve the repository's S3 permission prefix and qualify the zip by package."""
    identifier = release_id(repo, package, artifact, version)
    return f"{repo}/{identifier[len(repo) + 1 :]}.zip"
