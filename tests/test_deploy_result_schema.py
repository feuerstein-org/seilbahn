"""
Tests for `deploy-result.schema.json`, the shape deploy.yml writes per artifact.

These files are the last thing seilbahn controls before the version manifest is
committed to the CDK repo, so the schema is the point where a malformed value is
still seilbahn's problem. Several constraints here exist only to match
bergschacht's version-manifest.schema.json - see the drift tests at the bottom.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "deploy-result.schema.json"

IMAGE: dict[str, Any] = {
    "name": "api",
    "type": "image",
    "version": "1.2.0",
    "repository": "123.dkr.ecr.eu-central-1.amazonaws.com/feuerstein",
    "imageTag": "api-1.2.0",
}

LAMBDA: dict[str, Any] = {
    "name": "eom",
    "type": "lambda",
    "version": "1.2.0",
    "bucket": "feuerstein-lambda-artifacts",
    "key": "eom-1.2.0.zip",
    "objectVersion": "v1",
}


@pytest.fixture(scope="session")
def validator() -> Draft202012Validator:
    """The published schema, checked to be a valid schema in its own right."""
    schema: Any = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def errors_for(validator: Draft202012Validator, result: dict[str, Any]) -> list[str]:
    """Every schema error for one deploy result, as messages."""
    return [error.message for error in validator.iter_errors(result)]


def assert_valid(validator: Draft202012Validator, result: dict[str, Any]) -> None:
    """Assert a result is accepted, showing the errors if it is not."""
    errors = errors_for(validator, result)
    assert errors == [], f"expected valid, got: {errors}"


def without(result: dict[str, Any], key: str) -> dict[str, Any]:
    """The same result with one key removed."""
    return {k: v for k, v in result.items() if k != key}


# --------------------------------------------------------------------------
# What deploy.yml actually writes
# --------------------------------------------------------------------------


def test_the_docker_job_result_is_valid(validator: Draft202012Validator) -> None:
    """Exactly the object deploy.yml's `jq -nc` builds for an image."""
    assert_valid(validator, IMAGE)


def test_the_lambda_job_result_is_valid(validator: Draft202012Validator) -> None:
    """Exactly the object deploy.yml's `jq -nc` builds for a zip."""
    assert_valid(validator, LAMBDA)


# --------------------------------------------------------------------------
# Malformed results, caught before anything is committed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["name", "type", "version", "repository", "imageTag"])
def test_an_image_result_missing_any_field_is_rejected(validator: Draft202012Validator, key: str) -> None:
    """Every field feeds the manifest, so none of them is optional."""
    assert errors_for(validator, without(IMAGE, key))


@pytest.mark.parametrize("key", ["name", "type", "version", "bucket", "key", "objectVersion"])
def test_a_lambda_result_missing_any_field_is_rejected(validator: Draft202012Validator, key: str) -> None:
    """Each field is load-bearing; objectVersion pins the deploy to one upload."""
    assert errors_for(validator, without(LAMBDA, key))


def test_an_image_result_carrying_lambda_fields_is_rejected(validator: Draft202012Validator) -> None:
    """The two shapes are discriminated by `type`; mixing them is a bug upstream."""
    assert errors_for(validator, IMAGE | {"bucket": "b"})


def test_a_lambda_result_carrying_image_fields_is_rejected(validator: Draft202012Validator) -> None:
    """And the reverse."""
    assert errors_for(validator, LAMBDA | {"imageTag": "t"})


def test_an_unknown_artifact_type_is_rejected(validator: Draft202012Validator) -> None:
    """Unknown artifact type is rejected."""
    assert errors_for(validator, IMAGE | {"type": "binary"})


def test_an_empty_string_field_is_rejected(validator: Draft202012Validator) -> None:
    """A shell variable that expanded to nothing is the likely cause."""
    assert errors_for(validator, IMAGE | {"imageTag": ""})


@pytest.mark.parametrize("version", ["1.2", "v1.2.0", "1.2.0-rc1", "latest", ""])
def test_a_version_the_manifest_would_reject_is_rejected_here(
    validator: Draft202012Validator,
    version: str,
) -> None:
    r"""
    The manifest pins `version` to `\d+\.\d+\.\d+`, so anything else must fail here.

    Catching it later means it is already committed to the CDK repo.
    """
    assert errors_for(validator, IMAGE | {"version": version})


def test_an_unversioned_bucket_is_rejected(validator: Draft202012Validator) -> None:
    """
    `jq -r .VersionId` prints the string "null" when the bucket is unversioned.

    That is not a usable object version - the manifest entry would claim to pin a
    specific upload while pinning nothing - so it must not reach the CDK repo.
    """
    assert errors_for(validator, LAMBDA | {"objectVersion": "null"})


def test_a_name_the_manifest_would_reject_is_rejected_here(validator: Draft202012Validator) -> None:
    """The manifest's artifact keys allow no dots, so neither does this."""
    assert errors_for(validator, IMAGE | {"name": "my.artifact"})


# --------------------------------------------------------------------------
# Agreement with the config schema on the other side of the pipeline
# --------------------------------------------------------------------------


def test_artifact_name_patterns_match_the_config_schema(validator: Draft202012Validator) -> None:
    """
    Drift guard: a name accepted in seilbahn.toml must survive to the manifest.

    An artifact name is declared in seilbahn.toml, travels through a deploy
    result, and lands as a manifest key. If the config schema were the more
    permissive of the two, a repo could pass validation at the start of the
    pipeline and be rejected at the end - after the commit.
    """
    config_schema: Any = json.loads((SCHEMA_PATH.parent / "seilbahn.schema.json").read_text())
    result_schema: Any = validator.schema

    assert config_schema["$defs"]["artifactName"]["pattern"] == result_schema["$defs"]["artifactName"]["pattern"]
