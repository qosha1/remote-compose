# Copilot fixtures corpus

Snapshots of real AWS Copilot apps + canonical aws/copilot-cli e2e
fixtures, used by the `rc copilot import` test suite. The whole point
of this corpus is to prove the importer works on apps **other than**
sentinal — generality is the deliverable, not sentinal-specific shape
matching.

| fixture | source | patterns |
|---|---|---|
| `sentinal/` | live snapshot of `/Volumes/Veneno-External/.../sentinal/copilot` (15 services, 3 envs) | Backend Service, Worker Service, multi-env, multi-service, Service Connect, secretsmanager refs, env-var interpolation (`${COPILOT_ENVIRONMENT_NAME}`), pipelines |
| `external-shanikaediriweera/` | https://github.com/ShanikaEdiriweera/aws-copilot-example (2 services + 2 envs + 1 pipeline) | small real external app — Backend Service x2, env config, pipeline manifest |
| `aws-cli-lbws/` | aws/copilot-cli `e2e/multi-svc-app/copilot/front-end/manifest.yml` | canonical Load Balanced Web Service shape with build args + http section |
| `aws-cli-app-with-domain/` | aws/copilot-cli `e2e/app-with-domain/copilot/` (frontend + hello) | LBWS with `http.alias` custom-domain configuration |
| `aws-cli-static-site/` | aws/copilot-cli `e2e/static-site/copilot/` | Static Site type (CloudFront + S3 — distinct from container services; importer must detect + warn) |

## How to add to this corpus

1. Find a real Copilot app (public GitHub, customer migration, or aws-samples).
2. `rsync -a --exclude=.git --exclude=.DS_Store <src>/copilot/ tests/fixtures/copilot/<short-name>/`
3. Add a row to the table above with: source URL, what new patterns it exercises.
4. Run the importer's regression suite — the new fixture should either parse cleanly OR surface a concrete gap that becomes a sub-bead under the `rc-e5u.43` epic.

## Sanitization

These fixtures are SNAPSHOTS of real config. They contain Copilot's
secret-MANAGER REFERENCES (paths like
`secretsmanager:my-app/creds:KEY::`) — these are config-as-code
pointers, not the secret values themselves.

The original sentinal account ID has been replaced with the canonical
AWS docs placeholder `123456789012`. Re-snapshotting from a real
source (re-rsync the sentinal/) requires re-running the sanitization:

```bash
find tests/fixtures/copilot -type f \( -name "*.yml" -o -name "*.yaml" \) \
  | xargs sed -i '' 's/<real-account-id>/123456789012/g'
```

`tests/fixtures/copilot/sentinal/` was sourced from a private repo —
keep the snapshot here in sync only with the consent of the sentinal
team.
