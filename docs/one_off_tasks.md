# Running one-off commands (`rc run` / lifecycle `mode: task`)

Management commands — Django `migrate`, `sync_workflow_templates`, Rails
`db:seed`, etc. — often need the service's **Secrets Manager secrets** (DB
creds, API keys) to even import settings.

`rc exec` cannot provide those: it uses `aws ecs execute-command`, and the
SSM child process does **not** inherit the task's SM-injected secrets (only
the main container PID 1 gets them). So a secret-dependent command run via
`rc exec` dies at settings import (`DJANGO_AWS_ACCESS_KEY_ID unset`).

The fix is to run the command as a **fresh one-off task** on the service's
task definition — ECS injects the task role + the SM secrets exactly like a
normal container.

## `rc run`

```bash
rc run django -- python manage.py migrate
rc run django -- python manage.py sync_workflow_templates
rc run django --no-wait -- python manage.py some_long_job
```

`rc run` reuses the live service's task definition, network config, and
launch type, streams the task's CloudWatch logs, waits for it to stop, and
exits with the command's real exit code (so it fails a CI step on error).

## Lifecycle `mode: task`

Declare a hook with `mode: task` so `rc lifecycle <hook>` (and the deploy
auto-hook path) runs it as a one-off task instead of exec:

```yaml
services:
  django:
    lifecycle:
      migrate:
        command: ["python", "manage.py", "migrate", "--noinput"]
        mode: task          # default is 'exec'
      sync_templates:
        command: ["python", "manage.py", "sync_workflow_templates"]
        mode: task
```

`mode: exec` (the default) keeps the fast execute-command path for commands
that don't need secrets (an interactive shell, a quick health probe).

## Required IAM (the deploy/exec principal)

The one-off-task path calls `ecs:RunTask`, which in turn **passes** the task
definition's execution + task roles. The principal that runs `rc run` /
`rc lifecycle … mode: task` (e.g. a CI OIDC role) therefore needs:

```json
{
  "Effect": "Allow",
  "Action": ["ecs:RunTask", "ecs:DescribeTasks", "ecs:DescribeServices",
             "ecs:DescribeTaskDefinition"],
  "Resource": "*"
},
{
  "Effect": "Allow",
  "Action": "iam:PassRole",
  "Resource": [
    "arn:aws:iam::<account>:role/<project>-task",
    "arn:aws:iam::<account>:role/<project>-task-exec"
  ]
},
{
  "Effect": "Allow",
  "Action": "logs:GetLogEvents",
  "Resource": "*"
}
```

`iam:PassRole` is the one teams usually miss — without it `run_task` fails
with `AccessDeniedException … not authorized to perform: ecs:RunTask` (or a
PassRole denial). `logs:GetLogEvents` is only needed for the streamed output;
`rc run` degrades gracefully (no logs) if it's absent.
```
