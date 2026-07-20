# Seilbahn

Shared GitHub Actions reusable workflows and CI scripts for Feuerstein service repos.

## What's in here

```
.github/
  workflows/
    deploy.yml                              # reusable: build & publish docker/lambda artifacts, commit version manifest update
    test.yml                                # reusable: pre-commit + parallel pytest matrix per package
  actions/
    extract-config/
      action.yml                            # composite action wrapping the script below
      extract_config.py                     # reads workspace pyproject.toml's, emits changed artifacts
    update-version-manifest/
      action.yml
      update_version_manifest.py            # commits artifact metadata to the CDK repo's manifest after a deploy
```

## How callers use it

Each consumer repo should have something similar to the below:

`.github/workflows/deploy.yml`:

```yaml
name: Deploy

on:
  push:
    branches: [master]
  workflow_dispatch:
    inputs:
      redeploy_package_version:
        description: "Rebuild-only: release tag <package>/v<version> to rebuild & push to ECR/S3 (leave blank for a normal deploy)"
        type: string
        required: false
        default: ""

permissions:
  contents: write
  id-token: write

jobs:
  deploy:
    uses: feuerstein-org/seilbahn/.github/workflows/deploy.yml@v1
    secrets: inherit
    # On push this input is empty -> normal change-driven deploy.
    with:
      redeploy_package_version: ${{ inputs.redeploy_package_version }}
```

`.github/workflows/test.yml`:

```yaml
name: Test

on:
  pull_request:
  push:
    branches: [master]
  workflow_dispatch:

jobs:
  test:
    uses: feuerstein-org/seilbahn/.github/workflows/test.yml@v1
    secrets: inherit
```

## How a deploy decides what to ship (change detection)

On every push the deploy workflow runs **once, at the tip** of whatever was pushed. `extract-config` decides which packages to (re)deploy by comparing each workspace member's version `N` against the release tags on the remote (a single `git ls-remote --tags`):

- **tag absent** -> a new, unreleased version -> deploy and create the tag.
- **tag points at HEAD** -> a re-run or redeploy of this exact commit -> deploy again; the tag / ECR / S3 content checks make the rebuilds idempotent.
- **tag points at any other commit** -> `N` was already released elsewhere, so this is a no-op push -> skip.

> Note: If a push contains commits A (the version bump), B, C, the tag `<name>/v<N>` is created at **C** (the tip), because the build ships `tree-at-C` — including B's and C's changes — and the [redeploy path](#rebuilding-an-image-that-aged-out-of-ecr-redeploy) checks out the tag to reproduce that exact artifact. A version bump is the intent to release; the tag records the tree that was actually built.

## Rebuilding an image that aged out of ECR (redeploy)

GitHub only offers **Re-run** for ~30 days after a run, and old images are pruned from ECR by lifecycle policy. When you need a past image back - e.g. to pull and troubleshoot it locally - use the redeploy input instead of re-running:

1. In the consumer repo: **Actions -> Deploy -> Run workflow**.
2. Set `redeploy_package_version` to that version's tag (created at release time as `<package>/v<version>`, e.g. `myservice/v1.4.2`).

This runs a **rebuild-only** path: it checks out that tag, parses the package name from it, and rebuilds **every** artifact declared by that package, pushing them to ECR/S3. It deliberately **skips** tag creation and the version manifest update (and thus the CDK deploy), so the live environment is untouched - the images simply reappear in ECR for you to pull. (Rebuilds are content-checked, so artifacts still present in ECR/S3 are skipped rather than rebuilt.)

Note that for image Lambdas which show an "The function is trying to use a deleted image." error you need to manually update the Lambda config to point to essentially the same image (via tag). The reason is that the Lambda resolves the actual sha hash on deployment and a redeployed image doesn't guarantee the same has value to be produced (build time differences, base image changed etc.).

> Rolling the running environment *back* to an old version is intentionally not supported here: `update-version-manifest` only ever moves `latest` forward (`is_newer_version`). A rollback is a deliberate act performed in the CDK repo: add a `pinned` block with the old version to the entry in `version-manifests/latest.json`, commit and push.

## Required configuration in the consumer repo

### Variables (`vars.*`)

| Variable                | Used by  | Purpose                                                          |
| ----------------------- | -------- | ---------------------------------------------------------------- |
| `CICD_ACCOUNT_ID`       | deploy   | AWS account for ECR / S3 / OIDC role assumption                  |
| `AWS_REGION`            | deploy   | AWS region                                                       |
| `ECR_REPOSITORY_NAME`   | deploy   | ECR repo for docker artifacts                                    |
| `LAMBDA_S3_BUCKET_NAME` | deploy   | S3 bucket Lambda artifacts                                     |
| `CDK_REPO_OWNER`        | deploy   | GitHub owner of the CDK repo (normally `feuerstein-org`)    |
| `CDK_REPO_NAME`         | deploy   | CDK repo name (noramlly `bergschacht`) |

### Secrets

| Secret                     | Used by      | Purpose                                                                                  |
| -------------------------- | ------------ | ---------------------------------------------------------------------------------------- |
| `CDK_REPO_APP_ID`          | deploy       | GitHub App ID for committing manifest updates to the CDK repo (needs `contents: write`)  |
| `CDK_REPO_APP_PRIVATE_KEY` | deploy       | GitHub App private key                                                                   |
| `DEPS_APP_ID`              | test, deploy | GitHub App ID for cloning private workspace-org repos pulled in via `[tool.uv.sources]`  |
| `DEPS_APP_PRIVATE_KEY`     | test, deploy | GitHub App private key for the same                                                      |

Repository vars are inherited from the caller's context automatically. Secrets must be explicitly forwarded with `secrets: inherit` (or per-secret).

#### Docker artifacts that pull private deps

The `gh_deps_token` build secret is always passed to `docker/build-push-action`, but it's only consumed by Dockerfiles that explicitly mount it, use a `RUN --mount=type=secret` block if you have depend on private repos.

```dockerfile
RUN --mount=type=secret,id=gh_deps_token \
    git config --global url."https://x-access-token:$(cat /run/secrets/gh_deps_token)@github.com/".insteadOf "https://github.com/" \
 && uv sync --frozen --no-dev \
 && git config --global --unset url."https://x-access-token:$(cat /run/secrets/gh_deps_token)@github.com/".insteadOf
```

BuildKit keeps the secret out of the image layers and the build cache, so the token doesn't leak into the published image. The trailing `git config --unset` is belt-and-braces - strictly only needed if the same shell session does other git work afterwards.

### Environments

The deploy pipeline references two GitHub environments in the caller repo:

- `prod` - gates `deploy-docker-artifacts`, `deploy-lambda-artifacts`, `update-manifest`.
- `dev`  - gates the `test-python-packages` job in `test.yml`.

Create both environments (with whatever protection rules you want) in each consumer repo.

### OIDC role

The deploy workflow assumes an IAM role named `${repo}-prod-github-actions-role` via OIDC. Provisioning that role lives in the CDK repo. To create this role add your service repo [here](https://github.com/feuerstein-org/bergschacht/blob/master/lib/config/constants.ts).

## Contract with the consumer repo's layout

The deploy workflow assumes:

- **uv workspace.** `[tool.uv.workspace.members]` in the root `pyproject.toml`. Members are scanned for release-worthy version changes via their remote tags (see [change detection](#how-a-deploy-decides-what-to-ship-change-detection)).
- **Artifact declarations.** Each package's `pyproject.toml` declares deployable artifacts under `[tool.bergschacht.artifacts.<name>]`:

  ```toml
  [tool.bergschacht.artifacts.dagster]
  type = "docker"
  dockerfile = "docker/dagster.Dockerfile"

  [tool.bergschacht.artifacts.connector-example]
  type = "lambda"
  extra-files = ["collector.yaml"]   # optional; lambda only

  # One Dockerfile can publish several images via named multi-stage targets:
  [tool.bergschacht.artifacts.connector-example-ecs-container]
  type = "docker"
  dockerfile = "Dockerfile"
  target = "ecs"                     # optional; docker only

  [tool.bergschacht.artifacts.connector-example-lambda-image]
  type = "docker"
  dockerfile = "Dockerfile"
  target = "lambda"                     # optional; docker only
  ```

- **Docker build context** is always the repo root; `dockerfile` is package-relative.
- **`target`** (docker only) selects a named multi-stage build target (`docker build --target`), so one Dockerfile can publish multiple images that share builder stages. Omitted = final stage.
  Careful: an untargeted build produces the **last** stage, so when adding a second target to an existing Dockerfile, declare `target` explicitly on *both* artifacts.
- **Lambda packages** are installed via `uv pip install --target` from the package directory.
- **`extra-files`** (lambda only) lists package-relative paths copied into the zip alongside the Python install. The relative path is preserved, so `["collector.yaml"]` lands at `/var/task/collector.yaml`, `["configs/foo.yaml"]` lands at `/var/task/configs/foo.yaml`. Useful for ADOT collector configs or any non-Python runtime asset that can't ride along inside the wheel. Build fails if a declared path is missing.
- **mise tasks.** `test.yml` calls `mise run install-ci`, `mise run pre-commit-ci`, `mise run test-ci <package>`. Define these in `mise.ci.toml`.

## Known limitations (version manifest)

The version manifest lives as a git-committed file (`version-manifests/latest.json`) in the CDK repo, written by `update-version-manifest` via the GitHub contents API. Two known gaps are deliberately left open for now.

### 1. The manifest-write token can write anything in the CDK repo

`update-version-manifest` authenticates with a GitHub App installation token (`CDK_REPO_APP_ID` / `CDK_REPO_APP_PRIVATE_KEY`) scoped to the CDK repo with `contents: write`. GitHub App permissions are **per-repo, not per-path** - there is no way to grant "may write only `version-manifests/**`". So this token can commit arbitrary content **anywhere** in the CDK repo (all of the infrastructure-as-code, not just the manifest).

**Planned fix:** move the version manifests into their own dedicated repo and scope the token to that repo only, so the blast radius of a leaked token (or a compromised third-party action in the deploy job) is limited to manifest data rather than the deployable infrastructure.

### 2. Multiple manifest files are not supported

In the future when there are more CDK repos or multiple version manifests this workflow simply wont be able to realistically support it.

## Releasing

Consumers can pin to:

- `@v1` - floating major, gets all 1.x patches and minor additions.
- `@v1.2` - floating minor, gets 1.2.x patches only.
- `@v1.2.0` - immutable never moves.

`master` is the release branch - it always reflects the most recent release, with internal `uses: feuerstein-org/seilbahn/...@<ref>` lines pinned to the latest `vX.Y.Z`. Day-to-day edits to the composite action source (e.g. `extract_config.py`) land directly on `master`; edits to the workflow YAML files should go via a feature branch + PR so the release workflow's rewrite step has a known starting state.

The release workflow pushes commits that modify files under `.github/workflows/`, to do that a GitHub App is used (<https://github.com/organizations/feuerstein-org/settings/apps/feuerstein-seilbahn>).

App id and key are set as secrets on the `release` environment and can only be accessed by the master branch.

To cut a release, run the [`Release workflow`](.github/workflows/release.yml) via **Actions -> Release -> Run workflow**, passing the new semver tag (e.g. `v1.2.0`). The workflow:

1. Validates the version and refuses to overwrite an existing tag.
2. Rewrites every `uses: feuerstein-org/seilbahn/...@<ref>` in `.github/workflows/*.yml` to `@v1.2.0`.
3. Commits and pushes that to `master`.
4. Creates `v1.2.0`, force-moves `v1.2` and `v1` to that commit, pushes all three tags.
