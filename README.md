# Seilbahn

Shared GitHub Actions reusable workflows and CI scripts for Feuerstein service repos.

## What's in here

```
.github/
  workflows/
    deploy.yml                              # reusable: build & publish docker/lambda artifacts, update SSM, trigger CDK deploy
    test.yml                                # reusable: pre-commit + parallel pytest matrix per package
  actions/
    extract-config/
      action.yml                            # composite action wrapping the script below
      extract_config.py                     # reads workspace pyproject.toml's, emits changed artifacts
    update-ssm-manifest/
      action.yml
      update_ssm_manifest.py                # writes artifact metadata to SSM after a deploy
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

permissions:
  contents: write
  id-token: write

jobs:
  deploy:
    uses: feuerstein-org/seilbahn/.github/workflows/deploy.yml@v1
    secrets: inherit
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

## Required configuration in the consumer repo

### Variables (`vars.*`)

| Variable                | Used by  | Purpose                                                          |
| ----------------------- | -------- | ---------------------------------------------------------------- |
| `CICD_ACCOUNT_ID`       | deploy   | AWS account for ECR / S3 / SSM / OIDC role assumption            |
| `AWS_REGION`            | deploy   | AWS region                                                       |
| `ECR_REPOSITORY_NAME`   | deploy   | ECR repo for docker artifacts                                    |
| `LAMBDA_S3_BUCKET_NAME` | deploy   | S3 bucket Lambda artifacts                                     |
| `CDK_REPO_OWNER`        | deploy   | GitHub owner of the CDK repo (normally `feuerstein-org`)    |
| `CDK_REPO_NAME`         | deploy   | CDK repo name (noramlly `bergschacht`) |

### Secrets

| Secret                     | Used by      | Purpose                                                                                  |
| -------------------------- | ------------ | ---------------------------------------------------------------------------------------- |
| `CDK_REPO_APP_ID`          | deploy       | GitHub App ID for triggering workflows in CDK repo                                       |
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

- `prod` - gates `deploy-docker-artifacts`, `deploy-lambda-artifacts`, `update-manifest`, `trigger-deploy`.
- `dev`  - gates the `test-python-packages` job in `test.yml`.

Create both environments (with whatever protection rules you want) in each consumer repo.

### OIDC role

The deploy workflow assumes an IAM role named `${repo}-prod-github-actions-role` via OIDC. Provisioning that role lives in the CDK repo. To create this role add your service repo [here](https://github.com/feuerstein-org/bergschacht/blob/master/lib/config/constants.ts).

## Contract with the consumer repo's layout

The deploy workflow assumes:

- **uv workspace.** `[tool.uv.workspace.members]` in the root `pyproject.toml`. Members are scanned for version changes vs `HEAD~1`.
- **Artifact declarations.** Each package's `pyproject.toml` declares deployable artifacts under `[tool.bergschacht.artifacts.<name>]`:

  ```toml
  [tool.bergschacht.artifacts.dagster]
  type = "docker"
  dockerfile = "docker/dagster.Dockerfile"

  [tool.bergschacht.artifacts.connector-example]
  type = "lambda"
  extra-files = ["collector.yaml"]   # optional; lambda only
  ```

- **Docker build context** is always the repo root; `dockerfile` is package-relative.
- **Lambda packages** are installed via `uv pip install --target` from the package directory.
- **`extra-files`** (lambda only) lists package-relative paths copied into the zip alongside the Python install. The relative path is preserved, so `["collector.yaml"]` lands at `/var/task/collector.yaml`, `["configs/foo.yaml"]` lands at `/var/task/configs/foo.yaml`. Useful for ADOT collector configs or any non-Python runtime asset that can't ride along inside the wheel. Build fails if a declared path is missing.
- **mise tasks.** `test.yml` calls `mise run install-ci`, `mise run pre-commit-ci`, `mise run test-ci <package>`. Define these in `mise.ci.toml`.

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
