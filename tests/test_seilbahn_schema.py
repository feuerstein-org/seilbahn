"""
Tests for `seilbahn.schema.json`, the definition of the `seilbahn.toml` format.

The schema - not extract_config.py - decides whether a config is structurally
valid; the extract-config action validates against it before the script runs.
So the cases here are the ones a consumer hits when their config is wrong, and
they are the reason extract_config.py no longer carries key lists of its own.

The action validates with `check-jsonschema`, which is a wrapper over the same
`jsonschema` library used here, so accept/reject decisions match.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "seilbahn.schema.json"

MINIMAL = """
schema-version = 1
[packages.a]
path = "."
runtime = "python"
"""


@pytest.fixture(scope="session")
def validator() -> Draft202012Validator:
    """The published schema, checked to be a valid schema in its own right."""
    schema: Any = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def errors_for(validator: Draft202012Validator, config: str) -> list[str]:
    """Every schema error for a TOML string, as messages."""
    return [error.message for error in validator.iter_errors(tomllib.loads(config))]


def assert_valid(validator: Draft202012Validator, config: str) -> None:
    """Assert a config is accepted, showing the errors if it is not."""
    errors = errors_for(validator, config)
    assert errors == [], f"expected valid, got: {errors}"


def assert_invalid(validator: Draft202012Validator, config: str) -> list[str]:
    """Assert a config is rejected, returning the errors for further assertions."""
    errors = errors_for(validator, config)
    assert errors, "expected the schema to reject this config"
    return errors


# --------------------------------------------------------------------------
# Shapes that must keep working
# --------------------------------------------------------------------------


def test_the_minimal_config_is_valid(validator: Draft202012Validator) -> None:
    """One package with a path and a runtime is the whole requirement."""
    assert_valid(validator, MINIMAL)


def test_a_uv_workspace_with_a_library_and_images_is_valid(validator: Draft202012Validator) -> None:
    """Foerderturm's shape: [defaults] runtime, a library, and multiple images."""
    assert_valid(
        validator,
        """
schema-version = 1
[defaults]
runtime = "python"

[packages.ingestion-core]
path = "packages/ingestion-core"

[packages.foerderturm]
path = "packages/foerderturm"
artifacts.dagster   = { type = "docker", dockerfile = "docker/dagster.Dockerfile" }
artifacts.tailscale = { type = "docker", dockerfile = "docker/tailscale.Dockerfile" }
""",
    )


def test_one_name_published_as_both_flavors_is_valid(validator: Draft202012Validator) -> None:
    """`type = ["docker", "lambda"]` makes both key sets legal on one artifact."""
    assert_valid(
        validator,
        """
schema-version = 1
[packages.sample]
path = "."
runtime = "python"
artifacts.sample = { type = ["docker", "lambda"], build = "mise run build", dockerfile = "D" }
""",
    )


def test_every_optional_lambda_key_is_valid(validator: Draft202012Validator) -> None:
    """A lambda may declare output-dir and extra-files alongside build."""
    assert_valid(
        validator,
        """
schema-version = 1
[packages.svc]
path = "."
runtime = "python"

  [packages.svc.artifacts.svc]
  type = "lambda"
  build = "make zip"
  output-dir = "build/out"
  extra-files = ["configs/collector.yaml"]
""",
    )


# --------------------------------------------------------------------------
# What a consumer sees when the config is wrong
# --------------------------------------------------------------------------


def test_missing_schema_version_is_rejected(validator: Draft202012Validator) -> None:
    """Schema version is required."""
    errors = assert_invalid(validator, '[packages.a]\npath = "."\nruntime = "python"\n')
    assert any("schema-version" in error for error in errors)


def test_unsupported_schema_version_is_rejected(validator: Draft202012Validator) -> None:
    """A config written for a later seilbahn must not be silently accepted."""
    assert_invalid(validator, 'schema-version = 99\n[packages.a]\npath = "."\nruntime = "python"\n')


def test_no_packages_is_rejected(validator: Draft202012Validator) -> None:
    """A repo with nothing to release should not be running the pipeline."""
    assert_invalid(validator, "schema-version = 1\n[packages]\n")


def test_unknown_package_key_is_rejected(validator: Draft202012Validator) -> None:
    """A typo like `tests = true` would otherwise silently do nothing."""
    errors = assert_invalid(validator, MINIMAL + "tests = true\n")
    assert any("tests" in error for error in errors)


def test_unknown_top_level_key_is_rejected(validator: Draft202012Validator) -> None:
    """Unknown top level key is rejected."""
    assert_invalid(validator, MINIMAL + '\n[wat]\nx = "y"\n')


def test_unknown_runtime_is_rejected(validator: Draft202012Validator) -> None:
    """Only runtimes seilbahn knows where to read a version from are accepted."""
    errors = assert_invalid(validator, 'schema-version = 1\n[packages.a]\npath = "."\nruntime = "go"\n')
    assert any("'python', 'rust', 'node'" in error for error in errors)


def test_missing_runtime_without_defaults_is_rejected(validator: Draft202012Validator) -> None:
    """`runtime` is only optional when [defaults] supplies one."""
    assert_invalid(validator, 'schema-version = 1\n[packages.a]\npath = "."\n')


def test_missing_runtime_with_defaults_is_fine(validator: Draft202012Validator) -> None:
    """The other half of that rule - the fallback makes it optional."""
    assert_valid(validator, 'schema-version = 1\n[defaults]\nruntime = "rust"\n[packages.a]\npath = "."\n')


def test_missing_path_is_rejected(validator: Draft202012Validator) -> None:
    """Missing path is rejected."""
    assert_invalid(validator, 'schema-version = 1\n[packages.a]\nruntime = "python"\n')


def test_wrong_value_type_is_rejected(validator: Draft202012Validator) -> None:
    """A non-string path would reach the filesystem as nonsense."""
    errors = assert_invalid(validator, 'schema-version = 1\n[packages.a]\npath = 5\nruntime = "python"\n')
    assert any("not of type 'string'" in error for error in errors)


def test_unknown_artifact_type_is_rejected(validator: Draft202012Validator) -> None:
    """Unknown artifact type is rejected."""
    assert_invalid(validator, MINIMAL + 'artifacts.x = { type = "binary" }\n')


def test_docker_key_on_a_lambda_is_rejected(validator: Draft202012Validator) -> None:
    """A `dockerfile` on a lambda would otherwise be silently ignored."""
    assert_invalid(validator, MINIMAL + 'artifacts.x = { type = "lambda", build = "b", dockerfile = "D" }\n')


def test_lambda_key_on_a_docker_artifact_is_rejected(validator: Draft202012Validator) -> None:
    """And the reverse - `build` means nothing to an image."""
    assert_invalid(validator, MINIMAL + 'artifacts.x = { type = "docker", build = "b" }\n')


@pytest.mark.parametrize("runtime", ["python", "rust", "node"])
def test_lambda_without_build_is_rejected(validator: Draft202012Validator, runtime: str) -> None:
    """Every Lambda declares how its zip is filled - no runtime gets a free pass."""
    config = (
        f'schema-version = 1\n[packages.a]\npath = "."\nruntime = "{runtime}"\nartifacts.x = {{ type = "lambda" }}\n'
    )
    errors = assert_invalid(validator, config)
    assert any("build" in error for error in errors)


def test_a_name_that_would_break_its_release_tag_is_rejected(validator: Draft202012Validator) -> None:
    """Package names become `<name>/v<version>` tags and version-manifest keys."""
    assert_invalid(validator, 'schema-version = 1\n[packages."a/b"]\npath = "."\nruntime = "python"\n')


def test_every_error_is_reported_not_just_the_first(validator: Draft202012Validator) -> None:
    """A consumer migrating a repo should see every problem in one run."""
    errors = errors_for(
        validator,
        """
schema-version = 1
[packages.a]
path = "."
runtime = "go"

[packages.b]
path = 5
runtime = "python"
""",
    )

    assert len(errors) >= 2


# --------------------------------------------------------------------------
# The schema and the script must agree on what exists
# --------------------------------------------------------------------------


def test_schema_runtimes_match_the_scripts_version_sources(
    validator: Draft202012Validator,
    extract_config: Any,
) -> None:
    """
    Drift guard: the schema accepts exactly the runtimes the script can resolve.

    These are the one thing both sides still name. If a runtime is added to the
    schema without a version source, the script would accept the config and then
    fail with a KeyError at resolve time.
    """
    # A JSON Schema can legally be `true`/`false`, so the stub types this as
    # `bool | Mapping` - ours is always a mapping.
    schema: Any = validator.schema
    schema_runtimes: set[str] = set(schema["$defs"]["runtime"]["enum"])

    assert schema_runtimes == set(extract_config.RUNTIME_VERSION_SOURCES)
