# Seilbahn

Shared GitHub Actions reusable workflows and CI scripts for Feuerstein service repos.

## What's in here

```
.github/
  workflows/
    deploy.yml                              # reusable: build & publish docker/lambda artifacts, commit version manifest update
    test.yml                                # reusable: pre-commit + parallel test matrix per package
    ci.yml                                  # seilbahn's own tests (not for consumers)
  actions/
    extract-config/
      action.yml                            # composite action wrapping the script below
      extract_config.py                     # reads the consumer's seilbahn.toml, emits the deploy/test matrices
    update-version-manifest/
      action.yml
      update_version_manifest.py            # commits published artifact metadata to the CDK repo's manifest
```

Everything the pipeline builds is declared in the **consumer repo's root `seilbahn.toml`**. seilbahn itself is runtime-agnostic: Python, Rust, TypeScript repos and anything else that can produce a Docker image or a zip go through the same path. See [The `seilbahn.toml` contract](#the-seilbahntoml-contract).

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
    uses: feuerstein-org/seilbahn/.github/workflows/deploy.yml@v2
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
    uses: feuerstein-org/seilbahn/.github/workflows/test.yml@v2
    secrets: inherit
```

## How a deploy decides what to ship (change detection)

On every push the deploy workflow runs **once, at the tip** of whatever was pushed. `extract-config` decides which packages to (re)deploy by comparing each declared package's version `N` against the release tags on the remote (a single `git ls-remote --tags`):

- **tag absent** -> a new, unreleased version -> deploy and create the tag (plus its [major/minor aliases](#version-tags-and-their-aliases)).
- **tag points at HEAD** -> a re-run or redeploy of this exact commit -> deploy again; the tag / ECR / S3 content checks make the rebuilds idempotent.
- **tag points at any other commit** -> `N` was already released elsewhere, so this is a no-op push -> skip.

> Note: If a push contains commits A (the version bump), B, C, the tag `<name>/v<N>` is created at **C** (the tip), because the build ships `tree-at-C` — including B's and C's changes — and the [redeploy path](#rebuilding-an-image-that-aged-out-of-ecr-redeploy) checks out the tag to reproduce that exact artifact. A version bump is the intent to release; the tag records the tree that was actually built.
> If the **same package** is bumped twice across two commits that are pushed together - for example, A changes it to `v1.2.0` and B changes it to `v1.3.0` - the workflow still runs only once at B, sees only `v1.3.0`, and creates `<name>/v1.3.0` at B. The intermediate `v1.2.0` is not deployed or tagged. Push the commits separately if both versions must be released. Bumps to two different packages are handled together: each package's final version is deployed and tagged at the tip of the push.
> A pin in the version manifest doesn't block the artifact from being published, the `latest` property will still advance but whatever reads the manifest will always pick the pinned version.

## Version tags and their aliases

Full release tags are immutable - major/minor aliases follow the last release run, including reruns:

| Tag                    |  Moves? | Use it for                                                    |
| ---------------------- |  ------ | ------------------------------------------------------------- |
| `<name>/v1.4.2`        |  never  | Reproducible pins - redeploys, lockfiles.  |
| `<name>/v1.4`          |  yes    | Following patch releases of one minor line.                    |
| `<name>/v1`            |  yes    | Following everything in a major line.                          |

```toml
[tool.uv.sources]
# Follow the library's 0.x alias, uv.lock pins the resolved commit.
grundgeruest-telemetry-python = { git = "https://github.com/feuerstein-org/grundgeruest.git", subdirectory = "packages/grundgeruest-telemetry-python", tag = "grundgeruest-telemetry-python/v0" }
```

## Rebuilding an image that aged out of ECR (redeploy)

GitHub only offers **Re-run** for ~30 days after a run, and old images are pruned from ECR by lifecycle policy. When you need a past image back - e.g. to pull and troubleshoot it locally - use the redeploy input instead of re-running:

1. In the consumer repo: **Actions -> Deploy -> Run workflow**.
2. Set `redeploy_package_version` to that version's tag (created at release time as `<package>/v<version>`, e.g. `myservice/v1.4.2`). Pass the **exact** version here, not a `<package>/v1.4` or `<package>/v1` alias - the aliases move, so they would not reproduce a specific past artifact.

This runs a **rebuild-only** path: it checks out that tag, parses the package name from it, and rebuilds **every** artifact declared by that package, pushing them to ECR/S3. It deliberately **skips** tag creation and the version manifest update (and thus the CDK deploy), so the live environment is untouched - the images simply reappear in ECR for you to pull. (Rebuilds check whether the exact tag/key exists, so artifacts still present in ECR/S3 are skipped rather than rebuilt.)

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
| `DEPS_CLIENT_ID`        | test, deploy | GitHub App **Client ID** for cloning private workspace-org repos pulled in via `[tool.uv.sources]` |
| `CDK_REPO_CLIENT_ID`    | deploy   | GitHub App **Client ID** for committing manifest updates to the CDK repo. Set on the caller's `prod` environment |

### Secrets

| Secret                     | Used by      | Purpose                                                                                  |
| -------------------------- | ------------ | ---------------------------------------------------------------------------------------- |
| `CDK_REPO_APP_PRIVATE_KEY` | deploy       | GitHub App private key for committing manifest updates to the CDK repo (needs `contents: write`) |
| `DEPS_APP_PRIVATE_KEY`     | test, deploy | GitHub App private key for cloning private workspace-org repos                           |

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
- `dev`  - gates the `test-packages` job in `test.yml`.

Create both environments (with whatever protection rules you want) in each consumer repo.

### OIDC role

The deploy workflow assumes an IAM role named `${repo}-prod-github-actions-role` via OIDC. Provisioning that role lives in the CDK repo. To create this role add your service repo [here](https://github.com/feuerstein-org/bergschacht/blob/master/lib/config/constants.ts).

## The `seilbahn.toml` contract

A single `seilbahn.toml` at the **repo root** is the only file seilbahn reads for structure. It declares every package that gets versioned and released, and every artifact each one publishes. Seilbahn will make sure the TOML syntax is correct on every Action run.

```toml
#:schema https://raw.githubusercontent.com/Feuerstein-Org/seilbahn/v2/seilbahn.schema.json
schema-version = 2

[defaults]
runtime = "python"          # applied to any package that doesn't set its own

[packages.foerderturm]
path = "packages/foerderturm"
artifacts.dagster   = { type = "docker", dockerfile = "docker/dagster.Dockerfile" }
artifacts.tailscale = { type = "docker", dockerfile = "docker/tailscale.Dockerfile" }

[packages.ingestion-core]   # a library: versioned and tested, publishes nothing
path = "packages/ingestion-core"
```

A single-crate Rust repo, in full:

```toml
#:schema https://raw.githubusercontent.com/Feuerstein-Org/seilbahn/v2/seilbahn.schema.json
schema-version = 2

[packages.schmelzwerk]
path = "."
runtime = "rust"
artifacts.schmelzwerk = { type = "docker" }
```

**Catch mistakes before pushing**, by adding the same check to your `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/python-jsonschema/check-jsonschema
  rev: 0.33.0
  hooks:
    - id: check-jsonschema
      name: validate seilbahn.toml
      files: ^seilbahn\.toml$
      args:
        - --force-filetype=toml
        - --schemafile=https://raw.githubusercontent.com/Feuerstein-Org/seilbahn/v2/seilbahn.schema.json
```

### Artifact identity and naming

An artifact is identified by **repository, package, artifact name and type**.

| Destination | Generated name |
| --- | --- |
| Docker tag in the existing shared ECR repository | `<repo>.<package>.<artifact>.<version>` |
| Lambda key in the existing shared S3 bucket | `<repo>/<package>.<artifact>.<version>.zip` |
| Manifest entry | `repositories[repo].packages[package].images[artifact]` or `.lambdas[artifact]` |

### Package keys

| Key              | Required | Default          | Meaning                                                                       |
| ---------------- | -------- | ---------------- | ----------------------------------------------------------------------------- |
| *(table key)*    | yes      | -                | The package name. Used as the release tag prefix `<name>/v<version>` (see [Version tags](#version-tags-and-their-aliases)). |
| `path`           | yes      | -                | Repo-relative package directory. `"."` for a single-package repo.             |
| `runtime`        | yes*    | `defaults`       | `python`, `rust` or `node`. Selects where the version is read from.            |
| `test`           | no       | `true`           | Whether the packages tests will be run bei seilbahn |
| `test-id`        | no       | the table key    | Argument passed to `mise run test-ci <id>`.                                    |

\* required unless `[defaults] runtime` is set.

A package with no `artifacts` is a library: it is still change-detected, tagged and tested, but nothing is
published from it.

### Artifact keys

```toml
# docker
artifacts.api = { type = "docker", dockerfile = "Dockerfile", target = "api" }

# lambda - the repo always says how to fill the zip
artifacts.collector = { type = "lambda", build = "mise run build-lambda collector", extra-files = ["configs/otel.yaml"] }

# both lambda and docker, will publish 2 artifacts
artifacts.sample-python-repo = { type = ["docker", "lambda"], build = "mise run build-lambda" }
```

| Key           | Types  | Default             | Meaning                                                                 |
| ------------- | ------ | ------------------- | ----------------------------------------------------------------------- |
| `type`        | -      | -                   | `"docker"`, `"lambda"`, or a list of both.                              |
| `dockerfile`  | docker | `Dockerfile`        | Package-relative. The build **context is always the repo root**.        |
| `target`      | docker | final stage         | Named multi-stage target (`docker build --target`).                      |
| `build`       | lambda | **required**        | Shell command that fills `output-dir`.                                   |
| `output-dir`  | lambda | `dist/<artifact>`   | Repo-root-relative directory the `build` command populates.             |
| `extra-files` | lambda | `[]`                | Package-relative paths copied into the zip, preserving relative layout.  |

Declaring a docker-only key on a lambda (or the reverse) is an error.

> **`target` gotcha:** an untargeted build produces the **last** stage, so when adding a second target to an existing Dockerfile, declare `target` explicitly on *both* artifacts.

> **`extra-files`:** the relative path is preserved, so `["collector.yaml"]` lands at `/var/task/collector.yaml` and `["configs/foo.yaml"]` at `/var/task/configs/foo.yaml`. Useful for ADOT collector configs or any runtime asset that can't ride along inside the wheel. The build fails if a declared path is missing.

### How a Lambda zip gets filled

One way, for every runtime: the package declares `build`. seilbahn runs it from the repo root, then zips whatever landed in `output-dir` and uploads that. seilbahn never needs to know your toolchain, so a Lambda declaring no `build` is a config error rather than a fallback.

The build command runs under `mise` with the repo's `[tools]` available, so it can be a plain command (`cargo lambda build --release`) or a task. Note that `MISE_OVERRIDE_CONFIG_FILENAMES` pins mise to **`mise.ci.toml`** in CI, so a `mise run <task>` build must be defined there - same as `install-ci` and `test-ci`.

### mise tasks the workflows call

`test.yml` calls `mise run install-ci`, `mise run pre-commit-ci`, and `mise run test-ci <test-id>` for each package with `test = true`. Define these in `mise.ci.toml`. The matrix is language-agnostic - what a "test" means is entirely up to that task.

## Known limitations (version manifest)

The version manifest lives as a git-committed file (`version-manifests/latest.json`) in the CDK repo, written by `update-version-manifest` via the GitHub contents API. Two known gaps are deliberately left open for now.

### 1. The manifest-write token can write anything in the CDK repo

`update-version-manifest` authenticates with a GitHub App installation token (`CDK_REPO_CLIENT_ID` / `CDK_REPO_APP_PRIVATE_KEY`) scoped to the CDK repo with `contents: write`. GitHub App permissions are **per-repo, not per-path** - there is no way to grant "may write only `version-manifests/**`". So this token can commit arbitrary content **anywhere** in the CDK repo (all of the infrastructure-as-code, not just the manifest).

**Planned fix:** move the version manifests into their own dedicated repo and scope the token to that repo only, so the blast radius of a leaked token (or a compromised third-party action in the deploy job) is limited to manifest data rather than the deployable infrastructure.

### 2. Multiple manifest files are not supported

In the future when there are more CDK repos or multiple version manifests this workflow simply wont be able to realistically support it.

## Releasing

Consumers can pin to:

- `@v2` - floating major, gets all 2.x patches and minor additions.
- `@v2.1` - floating minor, gets 2.1.x patches only.
- `@v2.1.2` - immutable, never moves. Older exact tags retain their original schema contract.

`master` is the release branch - it always reflects the most recent release, with internal `uses: feuerstein-org/seilbahn/...@<ref>` lines pinned to the latest `vX.Y.Z`. Day-to-day edits to the composite action source (e.g. `extract_config.py`) land directly on `master`; edits to the workflow YAML files should go via a feature branch + PR so the release workflow's rewrite step has a known starting state.

The release workflow pushes commits that modify files under `.github/workflows/`, to do that a GitHub App is used (<https://github.com/organizations/feuerstein-org/settings/apps/feuerstein-seilbahn>).

App id and key are set as secrets on the `release` environment and can only be accessed by the master branch.

To cut a release, run the [`Release workflow`](.github/workflows/release.yml) via **Actions -> Release -> Run workflow**, passing a new, unused v2 semver tag (e.g. `v2.2.0`). The workflow:

1. Validates the version and refuses to overwrite an existing tag.
2. Rewrites every `uses: feuerstein-org/seilbahn/...@<ref>` in `.github/workflows/*.yml` to the supplied version.
3. Commits and pushes that to `master`.
4. Creates that immutable tag and advances its minor and major aliases (e.g. `v2.2` and `v2`) to the same commit.

> TODO: Appropriate pytest tests to be added.
