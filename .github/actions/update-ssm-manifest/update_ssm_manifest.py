#!/usr/bin/env python3
"""
Update version manifest in AWS SSM Parameter Store.

Reads deploy-result JSON files and writes artifact metadata to SSM,
but only if the deployed version is newer than the current one.
This script runs inside the application repo's deploy workflow.

Each artifact is stored as a single JSON-valued SSM parameter:
  /{ssmPrefix}/{repoName}/lambdas/{artifactName}    → {"bucket","key","version","commitSha"}
  /{ssmPrefix}/{repoName}/images/{artifactName}     → {"repository","imageTag","version","commitSha"}

Environment variables:
  REPO_NAME       - The repository name (e.g. sample-python-repo)
  COMMIT_SHA      - The git commit SHA
  SSM_PREFIX      - The SSM parameter prefix (e.g. /bergschacht), from the CDK repo name
  AWS_REGION      - The AWS region (set by configure-aws-credentials)

Inputs:
  --results-dir   - Path to directory containing deploy result JSON files
"""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3  # type: ignore[import]


def parse_version(version: str) -> tuple[int, ...]:
    """Parse a semantic version string into comparable tuple."""
    version = version.lstrip("v")
    parts = version.split(".")
    return tuple(int(p) for p in parts)


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


def get_current_version(ssm_client: boto3.client, param_name: str) -> str | None:
    """Get the current version from a JSON artifact parameter, or None if not found."""
    try:
        response = ssm_client.get_parameter(Name=param_name)
        data = json.loads(response["Parameter"]["Value"])
        return data.get("version")
    except ssm_client.exceptions.ParameterNotFound:
        return None


def update_artifact(
    ssm_client: boto3.client,
    param_name: str,
    artifact_json: dict,
    label: str,
) -> bool:
    """Write a single JSON artifact parameter. Returns True if updated."""
    current_version = get_current_version(ssm_client, param_name)
    new_version = artifact_json["version"]

    if not is_newer_version(new_version, current_version):
        print(f"Skipping {label}: deployed version {new_version} is not newer than SSM version {current_version}")
        return False

    ssm_client.put_parameter(
        Name=param_name,
        Value=json.dumps(artifact_json, separators=(",", ":")),
        Type="String",
        Overwrite=True,
    )
    print(f"Updated {label} from {current_version} to {new_version}")
    return True


def main() -> int:
    """Update SSM version manifest with deploy results."""
    parser = argparse.ArgumentParser(description="Update SSM version manifest after artifact deployment")
    parser.add_argument("--results-dir", required=True, help="Directory containing deploy result JSON files")

    args = parser.parse_args()

    repo_name = os.environ.get("REPO_NAME")
    commit_sha = os.environ.get("COMMIT_SHA")
    ssm_prefix = os.environ.get("SSM_PREFIX")

    if not all([repo_name, commit_sha, ssm_prefix]):
        print("Error: REPO_NAME, COMMIT_SHA, and SSM_PREFIX environment variables are required")
        return 1

    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(f"Error: Results directory not found: {results_dir}")
        return 1

    # Collect all deploy result files
    result_files = sorted(results_dir.glob("*.json"))
    if not result_files:
        print(f"Error: No deploy result JSON files found in {results_dir}")
        return 1

    # Parse all artifacts from deploy result files
    artifacts: list[dict[str, str]] = []
    for f in result_files:
        with f.open() as fh:
            artifacts.append(json.load(fh))

    ssm_client = boto3.client("ssm")

    updated = False

    for artifact in artifacts:
        a_name = artifact["name"]
        a_type = artifact["type"]
        label = f"{repo_name}/{a_name} ({a_type})"

        if a_type == "image":
            param_name = f"/{ssm_prefix}/{repo_name}/images/{a_name}"
            artifact_json = {
                "repository": artifact["repository"],
                "imageTag": artifact["imageTag"],
                "version": artifact["version"],
                "commitSha": commit_sha,
            }
        elif a_type == "lambda":
            param_name = f"/{ssm_prefix}/{repo_name}/lambdas/{a_name}"
            artifact_json = {
                "bucket": artifact["bucket"],
                "key": artifact["key"],
                "objectVersion": artifact["objectVersion"],
                "version": artifact["version"],
                "commitSha": commit_sha,
            }
        else:
            print(f"Error: Unknown artifact type '{a_type}' for artifact '{a_name}'")
            return 1

        updated |= update_artifact(ssm_client, param_name, artifact_json, label)

    # Write output for GitHub Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a") as f:
            f.write(f"updated={'true' if updated else 'false'}\n")

    if not updated:
        print("No updates made to SSM (all versions are current or older)")
        return 0

    print("Successfully updated SSM version manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
