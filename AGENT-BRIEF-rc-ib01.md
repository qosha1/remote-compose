# Brief: rc-ib01 — multi-container task groups

Repo: `remote-compose` (`~/Repos/devtools/remote-compose`), branch off `origin/main`.
Epic: **rc-ib01**. Run `bd show rc-ib01` and `bd ready` first.

## The problem, in one paragraph

`rc up --from-compose` renders **every compose service into its own ECS service**.
`remote_compose/provider/ecs/templates/services.tf.j2` line 52 hardcodes
`network_mode = "awsvpc"` with no option, and line 87 emits
`container_definitions = jsonencode([{` — a **single-element list**. So the renderer
structurally cannot produce a multi-container task, and every compose service burns
one branch ENI. Meanwhile `rc dev up` ships the compose file to a box and runs
`docker compose`, where 18 containers sit behind the host's one primary ENI. Same
tool, opposite networking models — and the prod half is the one that costs boxes.

## Evidence already gathered — do NOT re-derive this

Measured 2026-08-26 against account `033937118837`, `us-west-2`, cluster
`foundry-tenants` (5 tenants x 6 containers = 30 tasks):

- Real placement failure proving the ceiling is live, 2026-08-24, `tandem-django`:
  `Unexpected EC2 error while attempting to associate branch interface to trunk
  interface: AssociationLimitExceeded.`
- `awsvpcTrunking` is **enabled** account-wide; every container instance carries a
  trunk ENI (`ecs.awsvpc-trunk-id` present on all 6 boxes).
- rc's own `auto_size`, fed the real 30 task demands with `m6i` shapes
  `.with_trunking()`:

  | topology | shape | desired |
  |---|---|---|
  | 30 single-container tasks, 11520 MiB | m6i.large | **4** — matches the live fleet exactly |
  | 30 tasks | m6i.xlarge | 2 — same $/mo, so an instance resize alone buys nothing |
  | 10 grouped tasks (5 tenants x 2) | m6i.xlarge | **1** |
  | 10 grouped tasks | m6i.large | 2 |

  `m6i.large task_eni_slots=10`, `m6i.xlarge=20`.
- Memory dimension needs only 2 boxes. **ENI is 2x more demanding than memory.**
  Fleet-wide: 61.3 GB registered, 21.5 GB reserved, **5.9 GB actually used (10%)**.
- `auto_size` itself is CORRECT (rc-hguq shipped it; its ENI table matches AWS's
  published trunking numbers). **The topology is the bug, not the sizing.** Only
  `auto_size`'s *input* is wrong — `provider.py:2097` appends one `EC2TaskDemand`
  per service spec.

## Why grouping and not `network_mode = "bridge"`

Bridge is the literal dev-box answer and it kills the ENI dimension outright. It was
rejected on evidence, not taste: today each tenant has its own
`foundry-tenant-<slug>-tasks` SG whose only inbound rule is *all protocols, all
ports, from itself + the tenant's ALB SG*. Bridge puts every task on the host ENI
under one shared SG and **collapses all five tenants into one security boundary**.
Grouping keeps the per-tenant boundary and moves inter-container traffic to
localhost. If you find a way to keep per-tenant SGs under bridge, say so — that
would change the plan.

## Order of work

**Start with `rc-4seu` (design). It gates everything else and is the only ready bead.**

Then `rc-l6l8` (schema) → `rc-mvse` (multi-element container_definitions),
`rc-8xvk` (size from groups), `rc-m2sn` (per-container `essential`),
`rc-2zzd` (Cloud Map + ALB), `rc-93ol` (brownfield migration), `rc-qqje` (tests).

## The two things most likely to bite you

1. **`essential` is hardcoded `true` at services.tf.j2:96**, no conditional. A no-op
   for one container; in a group it means any container exiting stops the WHOLE
   task. Ship `rc-m2sn` with the grouping or you trade an ENI ceiling for a
   blast-radius regression — a frontend crash-loop would take down nginx and django.
   ECS also rejects a task where *all* containers are non-essential; validate that.
2. **Cloud Map.** `service_discovery.tf.j2` emits one A record per service, and the
   README's pitch is that compose hostnames (`db`, `cache`) keep resolving. In
   awsvpc a whole task shares ONE IP. **Verify before designing around it:** AWS
   documents `serviceRegistries` as a list but has historically limited an ECS
   service to ONE registry. If that limit is real, "register both `postgres.x.local`
   and `redis.x.local` against one grouped service" is dead and merged services need
   an app-side address change. Confirm against the API, not the docs prose.

## Rules for this repo

- **Ask the remote, not the working tree.** Checkouts here run behind. Use
  `git -C <repo> fetch -q origin && git -C <repo> show origin/main:<path>` before
  concluding anything about existing behaviour. Branching off `origin/main` says
  where to WRITE, not where to READ.
- `rc` CLAUDE.md: **bug fix = failing test first.** Golden fixtures live in
  `tests/fixtures/golden/`. A **group of one must render byte-identical to today's
  output** — that is the no-regression guard for every existing rc user.
- Preserve the `startsim-u88y` comment block in services.tf.j2 explaining why
  task-level `cpu` is omitted on EC2. It still applies: a group reserves memory,
  not CPU.
- `git rebase origin/main` before opening a PR. No AI attribution in commits.

## This brief may be wrong

It was written from one measurement session against one estate. Push back if:
the 2-task-per-tenant split is the wrong seam; the Cloud Map limit makes option (iii)
impossible and the whole DNS story needs rethinking; grouping turns out to break
`rc deploy`'s rolling-deploy math (`rc-anl6` models ENI slots needed mid-roll, and a
group changes that arithmetic); or `rc init --from-compose` can't express groups
without an ugly schema. A silently-executed bad brief looks exactly like success —
say so early rather than building around a bad premise.

Downstream consumer: `start-simpli` epic **startsim-pzumh**, child **startsim-8omm9**
is gated on this shipping. bd cannot express cross-store deps, so that gate is prose.
