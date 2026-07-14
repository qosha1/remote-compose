# Remote build backend — design (rc-8j7.5)

## Problem

`rc deploy` builds images **wherever the CLI runs**. On the ECS deploy path
that's an ephemeral GitHub Actions runner with a **cold** `rc-cache`
docker-container buildx builder. It builds N images serially, and each image —
even on a 100% cache hit — pays a full ECR registry-cache round-trip: import
cached layers, **re-export** the `mode=max` cache (~45s even when nothing
changed), then push. Measured **~12 min for 5 all-cached images**. On a
developer laptop the same build is seconds because the builder is warm and the
cache is local.

The `BuildBackend` seam (rc-8j7.1) makes **where** the build runs pluggable.
rc-8j7.3/.4/.6 already wrung out the on-runner path (parallel groups, cache-mode
knob, timing). This doc picks the **off-runner** backend to build next.

## Setup being optimized

- Heavy **Django** image (large pip + apt layers — the `mode=max` beneficiary)
  plus a **browser** image.
- ECR in **us-east-2**.
- Builder host = **ephemeral GH runner** (cold every deploy; the core problem).

## Options

| Axis | AWS CodeBuild | Persistent remote BuildKit (`buildx create --driver remote` → buildkitd on EC2/Fargate) | Depot.dev |
|---|---|---|---|
| Cold-start | Managed container spins per build (~10–30s); no infra idling | **Warmest** — daemon + its local cache already up | Warm managed builders (~seconds) |
| Warm cache across deploys | CodeBuild **local cache** (layer/custom) persists on the build host between runs; ECR cache optional | **Best** — buildkitd keeps its own local layer cache hot; no ECR cache round-trip needed at all | Persistent per-project cache managed for you |
| ECR push locality | **Same-region** (us-east-2) — push is in-region/fast | Same-region if the daemon runs in us-east-2 | Depot pushes to your ECR from their infra (cross-network egress) |
| Infra to babysit | **None** (fully managed; just a project + role) | **Most** — an EC2/Fargate daemon to run, patch, secure (mTLS), scale, and pay for 24/7 | None (SaaS) |
| Cost | Per-build-minute; $0 idle | EC2/Fargate **billed while up** even when idle | Per-minute SaaS subscription; data egress |
| Log streaming back to runner | CloudWatch Logs — tail via `StartBuild` + `GetLogEvents`/`logs tail` | buildkit progress streams over the gRPC session to the runner directly | Depot CLI streams logs to the runner |
| Auth/secrets | IAM role on the CodeBuild project (no long-lived creds) | mTLS certs to distribute + rotate | Depot token (third-party trust) |

## Recommendation

**Adopt AWS CodeBuild as the first remote backend.** For a cold ephemeral
runner pushing a heavy Django image to same-region ECR, CodeBuild gives the
biggest win for the least babysitting: it runs in us-east-2 (fast ECR push),
keeps a **local cache** hot between deploys (kills the mode=max ECR
round-trip that dominates our 12-min number), needs **zero idling infra** (vs a
remote BuildKit daemon we'd pay for and secure 24/7), uses an **IAM role**
instead of distributed mTLS certs or a third-party token, and streams logs via
CloudWatch. Keep a persistent remote BuildKit as the follow-up only if we later
need sub-10s warm starts and are willing to own the daemon; Depot is the
fallback if we want zero AWS-side work and accept cross-network ECR pushes plus
third-party trust.

## How it plugs into the `BuildBackend` seam

The seam contract (`remote_compose/image/backend.py`) is:

```python
class BuildBackend(ABC):
    def build_and_push(self, specs: list[ImageBuildSpec]) -> list[str]: ...
```

`AwsCodeBuildBackend` is already **registered and constructible** (a skeleton
that raises `NotImplementedError`), so `build.backend: aws-codebuild` resolves
today through `resolve_build_config`. To finish it, implement `build_and_push`:

1. Each `ImageBuildSpec` already carries everything a build needs — resolved
   `context`, `dockerfile`, `target`, `build_args`, `tags` (incl. `:latest`),
   `platform`, `cache_from`/`cache_to`, `cache_mode`, `push`. The backend
   translates the batch into a **buildspec** that runs the *same*
   `docker buildx build` args the local backend emits (single source of truth —
   factor the argv builder so both share it).
2. Ship the build context to CodeBuild (S3 source, or a `SourceVersion` git ref)
   and call **`codebuild:StartBuild`** — optionally one build per image group so
   groups still run **concurrently** (honoring `max_workers`), matching the
   local backend's parallelism and the deterministic input-order return.
3. Turn on CodeBuild **local cache** (`LOCAL_DOCKER_LAYER_CACHE` /
   `LOCAL_CUSTOM_CACHE`) so warm deploys skip the cold-builder tax; the
   buildspec still pushes to ECR (`--push` or `--load` + `docker push`, gated by
   `spec.push`).
4. **Stream logs** back to the runner by tailing the build's CloudWatch log
   group; poll `BatchGetBuilds` until each build reaches `SUCCEEDED`/`FAILED`.
5. Return the pushed service names in input order; raise on the first failed
   build so a broken group fails the deploy — identical semantics to
   `LocalBuildBackend`.

Registry auth and ECR-repo resolution stay in the provider exactly as now; the
backend only turns specs into pushed images. **No provider changes** are needed
to switch backends — only config: `provider_config.ecs.build.backend`,
`rc.yml` `build.backend`, or `RC_BUILD_BACKEND`.

## Implemented shape (rc-8j7.5)

`AwsCodeBuildBackend.build_and_push(specs)` (in `remote_compose/image/backend.py`)
now does the real work. Locked decisions:

- **Context delivery = S3.** rc tars the common-ancestor build-context root
  (honoring the root `.dockerignore`, best-effort) and uploads it **once** to
  `s3://<bucket>/rc-build-context/<project>/<uuid>.tar.gz`. One upload covers
  every image — the django context is `.`, the small images live under
  `compose/production/*` in the same tree, so each buildx invocation references
  its context path **relative to the extracted root**.
- **IAM = referenced, not created.** rc reads `service_role_arn` from config and
  **never** creates an IAM role. rc **does** ensure the CodeBuild *project*
  exists (create-if-missing, mirroring the buildcache-ECR-repo ensure), and
  create-if-missing the derived S3 source bucket when one isn't configured.
- **Same buildx, off the runner.** The generated buildspec runs the *same*
  `docker buildx build` args the local backend emits (factored into
  `buildx_build_flags()` — single source of truth), but with `--push` (CodeBuild
  auths to ECR directly, so no separate load+push) against the same ECR repos
  and the same `<buildcache>:<svc>-cache` refs.
- **Compute = `BUILD_GENERAL1_LARGE`** default; `LINUX_CONTAINER` /
  `aws/codebuild/standard:7.0` / `privilegedMode: true`.
- Logs stream from CloudWatch to rc's progress output; rc polls `BatchGetBuilds`
  to completion and **fails the deploy** (with the log tail) on any
  non-`SUCCEEDED` status.

### Config keys (`build.codebuild`)

Set under `provider_config.ecs.build` (canonical), `rc.yml` top-level `build`,
or the `RC_CODEBUILD_*` env override — same precedence as the other build knobs.

| Key | Default | Notes |
|---|---|---|
| `service_role_arn` | — (**required**) | IAM role the project runs as. Missing → clear config error before any AWS call. |
| `project_name` | `rc-build-<project>` | Created if missing, reused if present. |
| `compute_type` | `BUILD_GENERAL1_LARGE` | Any CodeBuild compute size. |
| `image` | `aws/codebuild/standard:7.0` | Managed Docker+buildx image. |
| `source_bucket` | `rc-build-source-<account>-<region>` (created if missing) | Prefer a pre-provisioned bucket in prod. |
| `region` | ECS region / parsed from the ECR tag host | — |
| `timeout_minutes` | `60` | Build wall-clock cap. |

## (a) One-time IAM service role

rc references this role; the operator creates it **once** per environment. Trust
policy (lets CodeBuild assume it):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "codebuild.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Least-privilege permissions policy (replace `<account>`, `<region>`, the ECR
repo paths, the source bucket, and `<project>`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EcrPushPullProjectRepos",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": [
        "arn:aws:ecr:<region>:<account>:repository/debuggai-api/*",
        "arn:aws:ecr:<region>:<account>:repository/debuggai-api-buildcache"
      ]
    },
    {
      "Sid": "S3ReadSource",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:GetBucketLocation",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::rc-build-source-<account>-<region>",
        "arn:aws:s3:::rc-build-source-<account>-<region>/*"
      ]
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:<region>:<account>:log-group:/aws/codebuild/rc-build-<project>",
        "arn:aws:logs:<region>:<account>:log-group:/aws/codebuild/rc-build-<project>:*"
      ]
    }
  ]
}
```

Note the **runner's own** credentials (not this role) upload the context, so the
runner's role additionally needs `s3:PutObject` on the source bucket and, if rc
must create the bucket/project, `s3:CreateBucket` + `codebuild:CreateProject`,
`codebuild:BatchGetProjects`, `codebuild:StartBuild`, `codebuild:BatchGetBuilds`,
and `logs:GetLogEvents`. Pre-provisioning the bucket + project lets you drop the
create perms.

## (b) Switch debuggai-api to CodeBuild

In the debuggai-api deploy config, add a `codebuild` block under the ECS build
knobs and flip the backend:

```yaml
provider_config:
  ecs:
    region: us-east-2
    build:
      backend: aws-codebuild
      codebuild:
        service_role_arn: arn:aws:iam::<account>:role/rc-codebuild-debuggai-api
        source_bucket: rc-build-source-<account>-us-east-2
        compute_type: BUILD_GENERAL1_LARGE
```

Everything else (ECR repos, buildcache repo, `--services`, `--tag` re-tag fast
path) is unchanged — only WHERE the build runs moves. To trial it from CI without
editing the config, set `RC_BUILD_BACKEND=aws-codebuild` +
`RC_CODEBUILD_ROLE_ARN=…` (+ `RC_CODEBUILD_SOURCE_BUCKET=…`) on the deploy job.
Revert instantly with `RC_BUILD_BACKEND=local` (the default).

## (c) Live-validation runbook

1. Provision the role (a) + (optionally) the source bucket + project once.
2. Baseline: run today's `local` deploy and note the **build+push total** line
   (`build+push total: …s`) — the ~12-min all-cached number.
3. Flip debuggai-api to `aws-codebuild` (b) and deploy. Watch rc's output: it
   prints `codebuild: packaged build context …`, `uploaded context to s3://…`,
   `started build …`, then streams `[codebuild] …` CloudWatch lines, and finally
   `build+push total (codebuild): …s`.
4. Confirm the images landed: `aws ecr describe-images --repository-name
   debuggai-api/django --image-ids imageTag=latest` shows a fresh `imagePushedAt`,
   and the ECS services roll onto the new `:latest`.
5. Compare the `build+push total (codebuild)` against the step-2 baseline. The
   **second** CodeBuild deploy is the real signal — the first primes CodeBuild's
   local layer cache; a warm cached build should beat the cold-runner ~12 min.
6. Failure path check: a broken build ends non-`SUCCEEDED`; rc raises with the
   log tail and the deploy fails (no half-pushed roll). Revert with
   `RC_BUILD_BACKEND=local`.

## Still needs a real-AWS / infra decision

- **Bucket lifecycle**: rc create-if-missing has no expiry on the source bucket;
  add an S3 lifecycle rule (expire `rc-build-context/` after N days) so old
  context tarballs don't accumulate. Prefer a pre-provisioned bucket in prod.
- **CodeBuild local cache**: the buildspec relies on the ECR registry cache
  (identical to local). Turning on CodeBuild `LOCAL_DOCKER_LAYER_CACHE` for an
  extra warm-cache win is a follow-up (project `cache` config) — measure first.
- **Intra-build parallelism**: images build sequentially in the buildspec
  (crisp fail-on-first-error; buildx already parallelizes layers within a build).
  Parallelizing whole images on one host contends for CPU/cache — revisit only if
  a multi-image deploy's wall-clock warrants it.
- Confirm us-east-2 CodeBuild availability + that `BUILD_GENERAL1_LARGE` has
  enough disk for the browser image (bump `compute_type` if not).
