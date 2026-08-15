"""
Tests for the update-version-manifest action script.

This script is the last step of a deploy: it advances `latest` in the CDK repo's
committed manifest, which is what actually triggers a CDK deploy. The risks it
covers here are moving a pointer backwards, clobbering a pin, and losing a
concurrent write from another repo's deploy.
"""

from __future__ import annotations

import base64
import json
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from tests.conftest import ReadOutputs

JsonObject = dict[str, Any]

ENV = {
    "REPO_NAME": "sample-python-repo",
    "COMMIT_SHA": "c0ffee",
    "SOURCE_REPO": "Feuerstein-Org/sample-python-repo",
    "MANIFEST_REPO": "Feuerstein-Org/schachtwerk",
    "MANIFEST_PATH": "version-manifests/latest.json",
    "MANIFEST_BRANCH": "master",
    "GITHUB_TOKEN": "token",
}


def image_result(name: str = "api", version: str = "1.2.0") -> JsonObject:
    """A deploy result as the docker job writes it."""
    return {
        "name": name,
        "type": "image",
        "repository": "123.dkr.ecr.eu-central-1.amazonaws.com/feuerstein",
        "imageTag": f"{name}-{version}",
        "version": version,
    }


def lambda_result(name: str = "eom", version: str = "1.2.0") -> JsonObject:
    """A deploy result as the lambda job writes it."""
    return {
        "name": name,
        "type": "lambda",
        "bucket": "feuerstein-lambda-artifacts",
        "key": f"{name}-{version}.zip",
        "objectVersion": "v1",
        "version": version,
    }


def no_sleep(_seconds: float) -> None:
    """Drop the retry backoff so the conflict tests do not actually wait."""


def entry_of(manifest: JsonObject, kind: str, name: str) -> JsonObject:
    """Dig out one artifact entry, for readable assertions."""
    entry: JsonObject = manifest["repositories"][ENV["REPO_NAME"]][kind][name]
    return entry


def apply(
    update_manifest: ModuleType, manifest: JsonObject, artifacts: list[JsonObject]
) -> tuple[bool, bool, list[str]]:
    """Run apply_updates with the standard env, mutating `manifest` in place."""
    return update_manifest.apply_updates(
        manifest,
        ENV["REPO_NAME"],
        ENV["SOURCE_REPO"],
        ENV["COMMIT_SHA"],
        artifacts,
    )


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "current", "expected"),
    [
        ("1.0.0", None, True),
        ("1.0.1", "1.0.0", True),
        ("1.10.0", "1.9.0", True),
        ("v2.0.0", "1.0.0", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0", "1.0.1", False),
        ("1.0.0", "not-a-version", False),
    ],
)
def test_is_newer_version(update_manifest: ModuleType, new: str, current: str | None, *, expected: bool) -> None:
    """Only a strictly newer, parseable version advances the pointer."""
    assert update_manifest.is_newer_version(new, current) is expected


def test_version_components_compare_numerically(update_manifest: ModuleType) -> None:
    """10 sorts above 9, which a string compare would get backwards."""
    assert update_manifest.parse_version("v1.10.2") == (1, 10, 2)


# --------------------------------------------------------------------------
# Building manifest payloads
# --------------------------------------------------------------------------


def test_image_result_becomes_an_images_entry(update_manifest: ModuleType) -> None:
    """Images entry carries the ECR coordinates plus the deploy's commit."""
    kind, payload = update_manifest.build_artifact(image_result(), "c0ffee")

    assert kind == "images"
    assert payload["imageTag"] == "api-1.2.0"
    assert payload["commitSha"] == "c0ffee"


def test_lambda_result_becomes_a_lambdas_entry(update_manifest: ModuleType) -> None:
    """Lambdas entry carries the S3 coordinates, including the object version."""
    kind, payload = update_manifest.build_artifact(lambda_result(), "c0ffee")

    assert kind == "lambdas"
    assert payload["bucket"] == "feuerstein-lambda-artifacts"
    assert payload["objectVersion"] == "v1"


def test_unknown_artifact_type_is_rejected(update_manifest: ModuleType) -> None:
    """A result the deploy jobs could not have written is a hard error."""
    with pytest.raises(ValueError, match="Unknown artifact type"):
        update_manifest.build_artifact({"name": "x", "type": "binary"}, "c0ffee")


def test_malformed_result_field_is_rejected(update_manifest: ModuleType) -> None:
    """A missing field fails here rather than writing null into the manifest."""
    broken = image_result()
    del broken["imageTag"]

    with pytest.raises(KeyError, match="imageTag"):
        update_manifest.build_artifact(broken, "c0ffee")


# --------------------------------------------------------------------------
# Advancing the manifest
# --------------------------------------------------------------------------


def test_first_deploy_creates_the_whole_path(update_manifest: ModuleType) -> None:
    """An empty manifest gets repositories/repo/kind/name built out for it."""
    manifest: JsonObject = {}

    deploy_changed, manifest_changed, labels = apply(update_manifest, manifest, [image_result()])

    assert (deploy_changed, manifest_changed, labels) == (True, True, ["api@1.2.0"])
    assert entry_of(manifest, "images", "api")["latest"]["version"] == "1.2.0"
    assert manifest["repositories"][ENV["REPO_NAME"]]["source"] == ENV["SOURCE_REPO"]


def test_older_version_does_not_move_latest(update_manifest: ModuleType) -> None:
    """A re-run of an old deploy must not roll the deployed version backwards."""
    manifest: JsonObject = {}
    apply(update_manifest, manifest, [image_result(version="2.0.0")])

    deploy_changed, manifest_changed, labels = apply(update_manifest, manifest, [image_result(version="1.0.0")])

    assert (deploy_changed, manifest_changed, labels) == (False, False, [])
    assert entry_of(manifest, "images", "api")["latest"]["version"] == "2.0.0"


def test_pinned_entry_advances_latest_without_deploying(update_manifest: ModuleType) -> None:
    """
    A pin is what deploys; latest is only a record of the newest build.

    So a pinned entry still tracks the new version, but must not report a deploy
    change - that flag is what gates the CDK pipeline.
    """
    manifest: JsonObject = {}
    apply(update_manifest, manifest, [image_result(version="1.0.0")])
    entry_of(manifest, "images", "api")["pinned"] = {"version": "1.0.0"}

    deploy_changed, manifest_changed, labels = apply(update_manifest, manifest, [image_result(version="2.0.0")])

    assert deploy_changed is False
    assert manifest_changed is True
    assert labels == []
    assert entry_of(manifest, "images", "api")["latest"]["version"] == "2.0.0"
    assert entry_of(manifest, "images", "api")["pinned"] == {"version": "1.0.0"}


def test_images_and_lambdas_live_under_separate_kinds(update_manifest: ModuleType) -> None:
    """One name can ship as both flavors without the two entries colliding."""
    manifest: JsonObject = {}

    _, _, labels = apply(update_manifest, manifest, [image_result("sample"), lambda_result("sample")])

    assert labels == ["sample@1.2.0", "sample@1.2.0"]
    assert entry_of(manifest, "images", "sample")["latest"]["imageTag"] == "sample-1.2.0"
    assert entry_of(manifest, "lambdas", "sample")["latest"]["key"] == "sample-1.2.0.zip"


def test_a_corrupted_manifest_fails_instead_of_being_overwritten(update_manifest: ModuleType) -> None:
    """
    A manifest that is not shaped like a manifest must stop the deploy.

    The tempting alternative - replace whatever is there with a fresh object and
    carry on - would commit that replacement, silently destroying every other
    repo's entries. Failing here leaves the committed manifest untouched.
    """
    manifest: JsonObject = {"repositories": "corrupted"}

    with pytest.raises(AttributeError):
        apply(update_manifest, manifest, [image_result()])

    assert manifest == {"repositories": "corrupted"}


# --------------------------------------------------------------------------
# Committing, with optimistic concurrency
# --------------------------------------------------------------------------


class FakeGitHub:
    """Stands in for the contents API, recording the PUTs it is given."""

    def __init__(self, manifest: JsonObject, conflicts: int = 0) -> None:
        """Serve `manifest`, rejecting the first `conflicts` writes with a 409."""
        self.manifest = manifest
        self.conflicts = conflicts
        self.puts: list[JsonObject] = []
        self.sha = "sha-0"

    def __call__(self, url: str, _token: str, method: str = "GET", body: JsonObject | None = None) -> JsonObject:
        """Handle one request the way the contents API would."""
        if method == "GET":
            encoded = base64.b64encode(json.dumps(self.manifest).encode()).decode()
            return {"content": encoded, "sha": self.sha}

        assert body is not None
        if self.conflicts > 0:
            self.conflicts -= 1
            # Someone else committed first; the next GET sees their sha.
            self.sha = f"sha-{len(self.puts)}-other"
            raise urllib.error.HTTPError(url, 409, "Conflict", {}, None)  # pyright: ignore[reportArgumentType]

        self.puts.append(body)
        self.manifest = json.loads(base64.b64decode(str(body["content"])))
        return {"commit": {"sha": "committed"}}


def run_main(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    github: FakeGitHub,
    results: list[JsonObject],
) -> int:
    """Invoke main() against a fake API and a directory of deploy results."""
    results_dir = tmp_path / "results"
    results_dir.mkdir(exist_ok=True)
    for index, result in enumerate(results):
        (results_dir / f"{index}.json").write_text(json.dumps(result))

    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(update_manifest, "github_request", github)
    monkeypatch.setattr("sys.argv", ["update_version_manifest.py", "--results-dir", str(results_dir)])

    return int(update_manifest.main())


def test_main_commits_and_reports_the_deploy(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outputs: ReadOutputs,
) -> None:
    """A successful advance commits once and sets updated=true for the CDK step."""
    github = FakeGitHub({})

    assert run_main(update_manifest, monkeypatch, tmp_path, github, [image_result()]) == 0

    assert len(github.puts) == 1
    assert "sample-python-repo artifacts (api@1.2.0)" in str(github.puts[0]["message"])
    assert outputs()["updated"] == "true"


def test_main_retries_a_conflicting_write(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outputs: ReadOutputs,
) -> None:
    """
    A 409 means another repo committed between our read and our write.

    Retrying from a fresh read is what stops one deploy from clobbering another,
    so the second attempt must carry the *new* base sha.
    """
    github = FakeGitHub({}, conflicts=1)
    monkeypatch.setattr("time.sleep", no_sleep)

    assert run_main(update_manifest, monkeypatch, tmp_path, github, [image_result()]) == 0

    assert len(github.puts) == 1
    assert github.puts[0]["sha"] == "sha-0-other"
    assert outputs()["updated"] == "true"


def test_main_gives_up_after_max_attempts(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Endless conflicts fail the step rather than looping forever."""
    github = FakeGitHub({}, conflicts=update_manifest.MAX_ATTEMPTS)
    monkeypatch.setattr("time.sleep", no_sleep)

    assert run_main(update_manifest, monkeypatch, tmp_path, github, [image_result()]) == 1
    assert github.puts == []


def test_main_writes_nothing_when_the_version_is_not_newer(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outputs: ReadOutputs,
) -> None:
    """A no-op redeploy must not create an empty manifest commit."""
    manifest: JsonObject = {}
    apply(update_manifest, manifest, [image_result(version="2.0.0")])
    github = FakeGitHub(manifest)

    assert run_main(update_manifest, monkeypatch, tmp_path, github, [image_result(version="1.0.0")]) == 0

    assert github.puts == []
    assert outputs()["updated"] == "false"


def test_main_requires_its_environment(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing token fails with a message naming what is absent."""
    github = FakeGitHub({})
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    for name, value in ENV.items():
        if name != "GITHUB_TOKEN":
            monkeypatch.setenv(name, value)
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "0.json").write_text(json.dumps(image_result()))
    monkeypatch.setattr(update_manifest, "github_request", github)
    monkeypatch.setattr("sys.argv", ["update_version_manifest.py", "--results-dir", str(results_dir)])

    assert update_manifest.main() == 1
    assert "GITHUB_TOKEN" in capsys.readouterr().out


def test_main_rejects_a_missing_results_dir(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing to publish is an error, not a silent success."""
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("sys.argv", ["update_version_manifest.py", "--results-dir", str(tmp_path / "nope")])

    assert update_manifest.main() == 1
    assert "Results directory not found" in capsys.readouterr().out


def test_main_rejects_an_empty_results_dir(
    update_manifest: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty results dir means the deploy jobs produced nothing."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("sys.argv", ["update_version_manifest.py", "--results-dir", str(results_dir)])

    assert update_manifest.main() == 1
    assert "No deploy result JSON files" in capsys.readouterr().out
