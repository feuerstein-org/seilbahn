"""
Tests for the extract-config action script.

These cover the two things a broken deploy would be worst at: mis-reading
`seilbahn.toml` (wrong artifacts built) and mis-deciding what changed (a release
skipped, or an already-released version rebuilt over the top of itself).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

if TYPE_CHECKING:
    from tests.conftest import ReadOutputs, WriteFile

# Mirrors extract_config.MatrixEntry. The scripts live in hyphenated directories
# under .github/, so they are loaded at runtime by path (see conftest) and cannot
# be imported for their types.
MatrixEntry = dict[str, str | list[str]]

# --------------------------------------------------------------------------
# Fixtures shaped like the repos that actually consume this
# --------------------------------------------------------------------------

PYPROJECT = """
[project]
name = "{name}"
version = "{version}"
"""

CARGO = """
[package]
name = "{name}"
version = "{version}"
"""

# foerderturm: a uv workspace - a library, a package with two images, and
# connectors that rely on the default Dockerfile path.
FOERDERTURM_CONFIG = """
schema-version = 1

[defaults]
runtime = "python"

[packages.ingestion-core]
path = "packages/ingestion-core"

[packages.foerderturm]
path = "packages/foerderturm"
artifacts.dagster   = { type = "docker", dockerfile = "docker/dagster.Dockerfile" }
artifacts.tailscale = { type = "docker", dockerfile = "docker/tailscale.Dockerfile" }

[packages.connector-example]
path = "packages/connector-example"
artifacts.connector-example-worker = { type = "docker" }
"""

FOERDERTURM_VERSIONS = {
    "ingestion-core": "0.3.0",
    "foerderturm": "0.7.0",
    "connector-example": "0.4.0",
}


@pytest.fixture
def foerderturm(write: WriteFile) -> None:
    """Write a foerderturm-shaped repo into the fake repo root."""
    write("seilbahn.toml", FOERDERTURM_CONFIG)
    for name, version in FOERDERTURM_VERSIONS.items():
        write(
            f"packages/{name}/pyproject.toml",
            PYPROJECT.format(name=name, version=version),
        )


def artifacts_by_name(entries: list[MatrixEntry]) -> dict[str, MatrixEntry]:
    """Index emitted artifact entries by name for readable assertions."""
    return {str(entry["name"]): entry for entry in entries}


def entries_of(package: Any) -> list[MatrixEntry]:
    """Every matrix entry a package contributes, in declaration order."""
    return [artifact.matrix_entry(package) for artifact in package.artifacts]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("foerderturm")
def test_reads_packages_and_versions(extract_config: ModuleType) -> None:
    """Reads packages and versions."""
    packages = extract_config.load_packages(Path.cwd())

    assert {p.name: p.version for p in packages} == FOERDERTURM_VERSIONS
    assert all(p.runtime == "python" for p in packages)


def test_inline_and_nested_tables_are_equivalent(extract_config: ModuleType, write: WriteFile) -> None:
    """
    The compact inline form is a writing style, not a second schema.

    tomllib produces the same dict either way, so both must yield identical
    artifact entries - otherwise the README's "write it either way" is a lie.
    """
    nested = """
schema-version = 1
[packages.app]
path = "."
runtime = "python"

  [packages.app.artifacts.api]
  type = "docker"
  dockerfile = "docker/api.Dockerfile"
"""
    inline = """
schema-version = 1
[packages.app]
path = "."
runtime = "python"
artifacts.api = { type = "docker", dockerfile = "docker/api.Dockerfile" }
"""
    write("pyproject.toml", PYPROJECT.format(name="app", version="1.0.0"))

    write("seilbahn.toml", nested)
    from_nested = extract_config.load_packages(Path.cwd())[0].artifacts

    write("seilbahn.toml", inline)
    from_inline = extract_config.load_packages(Path.cwd())[0].artifacts

    assert from_nested == from_inline
    assert from_nested == [extract_config.DockerArtifact(name="api", dockerfile="docker/api.Dockerfile", target="")]


def test_rust_package_reads_version_from_cargo_toml(extract_config: ModuleType, write: WriteFile) -> None:
    """Rust package reads version from cargo toml."""
    write(
        "seilbahn.toml",
        """
schema-version = 1
[packages.schmelzwerk]
path = "."
runtime = "rust"
artifacts.schmelzwerk = { type = "docker" }
""",
    )
    write("Cargo.toml", CARGO.format(name="schmelzwerk", version="0.1.0"))

    (package,) = extract_config.load_packages(Path.cwd())

    assert package.version == "0.1.0"
    assert package.runtime == "rust"


def test_node_package_reads_version_from_package_json(extract_config: ModuleType, write: WriteFile) -> None:
    """Node package reads version from package json."""
    write(
        "seilbahn.toml",
        """
schema-version = 1
[packages.web]
path = "apps/web"
runtime = "node"
""",
    )
    write("apps/web/package.json", json.dumps({"name": "web", "version": "2.1.3"}))

    (package,) = extract_config.load_packages(Path.cwd())

    assert package.version == "2.1.3"


# --------------------------------------------------------------------------
# Artifact expansion and defaults
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("foerderturm")
def test_docker_defaults_are_resolved_in_python(extract_config: ModuleType) -> None:
    """
    Defaults are resolved at parse time, not with `||` fallbacks in workflow YAML.

    deploy.yml consumes `dockerfile` and `target` directly, so an undeclared
    dockerfile must arrive as "Dockerfile" and an undeclared target as "".
    """
    packages = {p.name: p for p in extract_config.load_packages(Path.cwd())}
    package = packages["connector-example"]

    assert entries_of(package) == [
        {
            "name": "connector-example-worker",
            "type": "docker",
            "version": "0.4.0",
            "package_path": "packages/connector-example",
            "runtime": "python",
            "dockerfile": "Dockerfile",
            "target": "",
        }
    ]


def test_list_type_fans_out_to_one_artifact_per_type(extract_config: ModuleType, write: WriteFile) -> None:
    """
    sample-python-repo publishes one artifact name as both an image and a zip.

    The name is the manifest key bergschacht's CDK looks up, so this has to keep
    working - splitting it into two names would be a breaking rename.
    """
    write(
        "seilbahn.toml",
        """
schema-version = 1
[packages.sample-python-repo]
path = "."
runtime = "python"
artifacts.sample-python-repo = { type = ["docker", "lambda"], build = "mise run build-lambda" }
""",
    )
    write("pyproject.toml", PYPROJECT.format(name="sample-python-repo", version="1.2.0"))

    (package,) = extract_config.load_packages(Path.cwd())

    entries = entries_of(package)
    assert [e["type"] for e in entries] == ["docker", "lambda"]
    assert {str(e["name"]) for e in entries} == {"sample-python-repo"}
    assert entries[0]["dockerfile"] == "Dockerfile"
    assert entries[1]["build"] == "mise run build-lambda"
    assert entries[1]["output_dir"] == "dist/sample-python-repo"
    assert entries[1]["extra_files"] == []


def test_lambda_carries_its_build_command_and_default_output_dir(extract_config: ModuleType, write: WriteFile) -> None:
    """Lambda carries its build command and default output dir."""
    write(
        "seilbahn.toml",
        """
schema-version = 1
[packages.schmelzwerk]
path = "."
runtime = "rust"
artifacts.eom-runner = { type = "lambda", build = "mise run build-lambda eom-runner" }
""",
    )
    write("Cargo.toml", CARGO.format(name="schmelzwerk", version="0.1.0"))

    (package,) = extract_config.load_packages(Path.cwd())

    (entry,) = entries_of(package)
    assert entry["build"] == "mise run build-lambda eom-runner"
    assert entry["output_dir"] == "dist/eom-runner"
    assert entry["extra_files"] == []


def test_extra_files_and_output_dir_overrides(extract_config: ModuleType, write: WriteFile) -> None:
    """Extra files and output dir overrides."""
    write(
        "seilbahn.toml",
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
    write("pyproject.toml", PYPROJECT.format(name="svc", version="1.0.0"))

    (package,) = extract_config.load_packages(Path.cwd())

    (entry,) = entries_of(package)
    assert entry["extra_files"] == ["configs/collector.yaml"]
    assert entry["output_dir"] == "build/out"


# --------------------------------------------------------------------------
# Validation - these are the messages a consumer sees when their config is wrong
# --------------------------------------------------------------------------


def assert_fails_with(extract_config: ModuleType, capsys: pytest.CaptureFixture[str], fragment: str) -> None:
    """Run load_packages and assert it exits with an annotation containing `fragment`."""
    with pytest.raises(SystemExit) as excinfo:
        extract_config.load_packages(Path.cwd())
    assert excinfo.value.code == 1
    output = capsys.readouterr().out
    assert "::error::" in output
    assert fragment in output, f"expected {fragment!r} in:\n{output}"


@pytest.mark.usefixtures("repo")
def test_missing_config_file_is_a_clear_error(extract_config: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing config file is a clear error."""
    assert_fails_with(extract_config, capsys, "No seilbahn.toml at the repo root")


def test_missing_package_path_on_disk_is_rejected(
    extract_config: ModuleType, write: WriteFile, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing package path on disk is rejected."""
    write(
        "seilbahn.toml",
        'schema-version = 1\n[packages.a]\npath = "packages/nope"\nruntime = "python"\n',
    )
    assert_fails_with(extract_config, capsys, "does not exist")


def test_missing_version_manifest_is_rejected(
    extract_config: ModuleType, write: WriteFile, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing version manifest is rejected."""
    write(
        "seilbahn.toml",
        'schema-version = 1\n[packages.a]\npath = "."\nruntime = "python"\n',
    )
    assert_fails_with(extract_config, capsys, "expects the version in ./pyproject.toml")


def test_all_errors_are_reported_at_once(
    extract_config: ModuleType, write: WriteFile, capsys: pytest.CaptureFixture[str]
) -> None:
    """A consumer migrating a repo should see every problem in one run."""
    write(
        "seilbahn.toml",
        """
schema-version = 1
[packages.a]
path = "packages/missing-a"
runtime = "python"

[packages.b]
path = "packages/missing-b"
runtime = "python"
""",
    )
    with pytest.raises(SystemExit):
        extract_config.load_packages(Path.cwd())

    output = capsys.readouterr().out
    assert output.count("::error::") == 2


# --------------------------------------------------------------------------
# Change detection
# --------------------------------------------------------------------------

HEAD = "a" * 40
OTHER = "b" * 40


@pytest.mark.usefixtures("foerderturm")
def test_untagged_version_is_a_new_release(extract_config: ModuleType) -> None:
    """Untagged version is a new release."""
    packages = extract_config.load_packages(Path.cwd())

    artifacts, changed = extract_config.collect_changed_artifacts(packages, {}, HEAD)

    assert {p["name"] for p in changed} == set(FOERDERTURM_VERSIONS)
    assert set(artifacts_by_name(artifacts)) == {
        "dagster",
        "tailscale",
        "connector-example-worker",
    }


@pytest.mark.usefixtures("foerderturm")
def test_tag_on_head_redeploys(extract_config: ModuleType) -> None:
    """Tag on head redeploys."""
    packages = extract_config.load_packages(Path.cwd())
    tags = {f"{name}/v{v}": HEAD for name, v in FOERDERTURM_VERSIONS.items()}

    _, changed = extract_config.collect_changed_artifacts(packages, tags, HEAD)

    assert len(changed) == len(FOERDERTURM_VERSIONS)


@pytest.mark.usefixtures("foerderturm")
def test_tag_on_another_commit_is_skipped(extract_config: ModuleType) -> None:
    """Tag on another commit is skipped."""
    packages = extract_config.load_packages(Path.cwd())
    tags = {f"{name}/v{v}": OTHER for name, v in FOERDERTURM_VERSIONS.items()}

    artifacts, changed = extract_config.collect_changed_artifacts(packages, tags, HEAD)

    assert changed == []
    assert artifacts == []


@pytest.mark.usefixtures("foerderturm")
def test_only_the_bumped_package_is_released(extract_config: ModuleType) -> None:
    """Only the bumped package is released."""
    packages = extract_config.load_packages(Path.cwd())
    tags = {f"{name}/v{v}": OTHER for name, v in FOERDERTURM_VERSIONS.items() if name != "foerderturm"}

    artifacts, changed = extract_config.collect_changed_artifacts(packages, tags, HEAD)

    assert [p["name"] for p in changed] == ["foerderturm"]
    assert set(artifacts_by_name(artifacts)) == {"dagster", "tailscale"}


@pytest.mark.usefixtures("foerderturm")
def test_library_package_is_tagged_but_publishes_nothing(extract_config: ModuleType) -> None:
    """ingestion-core is versioned and tagged, but ships no artifact."""
    packages = extract_config.load_packages(Path.cwd())
    tags = {f"{name}/v{v}": OTHER for name, v in FOERDERTURM_VERSIONS.items() if name != "ingestion-core"}

    artifacts, changed = extract_config.collect_changed_artifacts(packages, tags, HEAD)

    assert [p["name"] for p in changed] == ["ingestion-core"]
    assert artifacts == []


# --------------------------------------------------------------------------
# Redeploy
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("foerderturm")
def test_redeploy_rebuilds_every_artifact_of_one_package(extract_config: ModuleType) -> None:
    """Redeploy rebuilds every artifact of one package."""
    packages = extract_config.load_packages(Path.cwd())

    artifacts, changed = extract_config.collect_redeploy_package_version(packages, "foerderturm/v0.7.0")

    assert [p["name"] for p in changed] == ["foerderturm"]
    assert set(artifacts_by_name(artifacts)) == {"dagster", "tailscale"}


@pytest.mark.usefixtures("foerderturm")
def test_redeploy_rejects_a_malformed_tag(extract_config: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """Redeploy rejects a malformed tag."""
    with pytest.raises(SystemExit):
        extract_config.collect_redeploy_package_version(packages_of(extract_config), "foerderturm-0.7.0")
    assert "is not of the form" in capsys.readouterr().out


@pytest.mark.usefixtures("foerderturm")
def test_redeploy_rejects_an_undeclared_package(extract_config: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """Redeploy rejects an undeclared package."""
    with pytest.raises(SystemExit):
        extract_config.collect_redeploy_package_version(packages_of(extract_config), "ghost/v1.0.0")
    assert "is not declared in seilbahn.toml" in capsys.readouterr().out


def packages_of(extract_config: ModuleType) -> list[Any]:
    """Load packages from the current fake repo."""
    return extract_config.load_packages(Path.cwd())


# --------------------------------------------------------------------------
# Modes and outputs
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("foerderturm")
def test_deploy_mode_writes_split_matrices(
    extract_config: ModuleType, outputs: ReadOutputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deploy mode writes split matrices."""
    monkeypatch.setattr(extract_config, "get_released_tags", dict)
    monkeypatch.setattr(extract_config, "get_head_sha", lambda: HEAD)
    monkeypatch.delenv("REDEPLOY_PACKAGE_VERSION", raising=False)
    monkeypatch.setenv("SEILBAHN_MODE", "deploy")

    extract_config.main()

    written = outputs()
    assert written["has_changes"] == "true"
    assert len(json.loads(written["docker_artifacts"])) == 3
    assert json.loads(written["lambda_artifacts"]) == []
    assert len(json.loads(written["packages"])) == 3


def test_test_mode_lists_opted_in_packages(
    extract_config: ModuleType, write: WriteFile, outputs: ReadOutputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test mode lists opted in packages."""
    write(
        "seilbahn.toml",
        """
schema-version = 1

[defaults]
runtime = "python"

[packages.app]
path = "packages/app"

[packages.docs]
path = "packages/docs"
test = false

[packages.tools]
path = "packages/tools"
test-id = "tools-suite"
""",
    )
    for name in ("app", "docs", "tools"):
        write(
            f"packages/{name}/pyproject.toml",
            PYPROJECT.format(name=name, version="1.0.0"),
        )
    monkeypatch.setenv("SEILBAHN_MODE", "test")

    extract_config.main()

    written = outputs()
    assert json.loads(written["test_packages"]) == ["app", "tools-suite"]
    assert written["has_tests"] == "true"


@pytest.mark.usefixtures("foerderturm")
def test_test_mode_needs_no_git(
    extract_config: ModuleType, outputs: ReadOutputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test matrix must not touch the remote - it runs on every PR."""

    def explode(*_args: object, **_kwargs: object) -> NoReturn:
        msg = "test mode must not call git"
        raise AssertionError(msg)

    monkeypatch.setattr(extract_config, "get_released_tags", explode)
    monkeypatch.setattr(extract_config, "get_head_sha", explode)
    monkeypatch.setenv("SEILBAHN_MODE", "test")

    extract_config.main()

    assert outputs()["has_tests"] == "true"


@pytest.mark.usefixtures("foerderturm")
def test_unknown_mode_is_rejected(
    extract_config: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown mode is rejected."""
    monkeypatch.setenv("SEILBAHN_MODE", "publish")

    with pytest.raises(SystemExit):
        extract_config.main()

    assert "Unknown mode 'publish'" in capsys.readouterr().out
