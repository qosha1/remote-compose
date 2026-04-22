"""In-memory reference Provider used by contract tests and higher-level mocks.

FakeProvider is the baseline: every test that does not depend on real network
behavior (public ingress, persistent volumes, cross-service DNS) must pass
against FakeProvider. Real providers then run the same suite and either pass
(proving conformance) or fail (proving they are wrong).
"""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path
from typing import Any, Iterator, Optional

from . import register
from .base import (
    DeployContext,
    DeployResult,
    ExecResult,
    PlanResult,
    Provider,
    ProviderError,
    ProviderNotFoundError,
    ServiceStatus,
    StatusReport,
)


_id_counter = itertools.count(1)


def _next_revision_id(project: str) -> str:
    return f"{project}-rev-{next(_id_counter)}"


def _config_hash(ctx: DeployContext) -> str:
    parts: list[Any] = [
        ctx.project,
        sorted(
            (
                n,
                s.cpu,
                s.memory,
                s.replicas,
                s.type,
                s.launch_type or "",
                s.health_check_path or "",
                s.public,
                s.port or 0,
            )
            for n, s in ctx.services.items()
        ),
        sorted(
            (s.name, s.source, s.path or "", s.arn or "", s.ref or "")
            for s in ctx.secrets
        ),
    ]
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:12]


class _Revision:
    def __init__(
        self,
        revision_id: str,
        config_hash: str,
        services: dict[str, dict[str, Any]],
    ) -> None:
        self.revision_id = revision_id
        self.config_hash = config_hash
        self.services = services


class _ProjectState:
    def __init__(self) -> None:
        self.revisions: list[_Revision] = []
        self.active: Optional[_Revision] = None


class FakeProvider(Provider):
    name = "fake"

    _projects: dict[str, _ProjectState] = {}
    _pending_faults: set[str] = set()

    @classmethod
    def reset(cls) -> None:
        cls._projects = {}
        cls._pending_faults = set()

    def inject_fault_once(self, where: str) -> None:
        type(self)._pending_faults.add(where)

    def _state(self, ctx: DeployContext) -> _ProjectState:
        return self._projects.setdefault(ctx.project, _ProjectState())

    def _run_services(self, ctx: DeployContext) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "desired": spec.replicas,
                "running": spec.replicas,
                "health": "healthy",
            }
            for name, spec in ctx.services.items()
        }

    def deploy(self, ctx: DeployContext) -> DeployResult:
        if "mid_deploy" in self._pending_faults:
            self._pending_faults.remove("mid_deploy")
            raise ProviderError("injected fault: mid_deploy")

        state = self._state(ctx)
        cfg_hash = _config_hash(ctx)

        if state.active is not None and state.active.config_hash == cfg_hash:
            return DeployResult(
                revision_id=state.active.revision_id,
                services=list(state.active.services.keys()),
                duration_s=0.0,
            )

        revision = _Revision(
            revision_id=_next_revision_id(ctx.project),
            config_hash=cfg_hash,
            services=self._run_services(ctx),
        )
        state.revisions.append(revision)
        state.active = revision
        return DeployResult(
            revision_id=revision.revision_id,
            services=list(revision.services.keys()),
            duration_s=0.01,
        )

    def redeploy(
        self,
        ctx: DeployContext,
        services: Optional[list[str]] = None,
    ) -> DeployResult:
        state = self._state(ctx)
        if state.active is None:
            return self.deploy(ctx)
        revision = _Revision(
            revision_id=_next_revision_id(ctx.project),
            config_hash=_config_hash(ctx),
            services=self._run_services(ctx),
        )
        state.revisions.append(revision)
        state.active = revision
        return DeployResult(
            revision_id=revision.revision_id,
            services=list(revision.services.keys()),
            duration_s=0.01,
        )

    def plan(self, ctx: DeployContext) -> PlanResult:
        state = self._state(ctx)
        if state.active is None:
            return PlanResult(
                create=len(ctx.services), update=0, destroy=0,
                raw_plan="fake plan: initial",
            )
        if _config_hash(ctx) == state.active.config_hash:
            return PlanResult(create=0, update=0, destroy=0, raw_plan="no changes")
        current = set(state.active.services.keys())
        desired = set(ctx.services.keys())
        return PlanResult(
            create=len(desired - current),
            update=len(current & desired),
            destroy=len(current - desired),
            raw_plan="fake plan: diff",
        )

    def status(self, ctx: DeployContext) -> StatusReport:
        state = self._state(ctx)
        if state.active is None:
            return StatusReport(services=[], cluster_health="inactive")
        services = [
            ServiceStatus(
                name=n,
                desired=s["desired"],
                running=s["running"],
                health=s["health"],
            )
            for n, s in state.active.services.items()
        ]
        return StatusReport(services=services, cluster_health="healthy")

    def logs(
        self,
        ctx: DeployContext,
        service: str,
        follow: bool = False,
        tail: int = 100,
    ) -> Iterator[str]:
        state = self._state(ctx)
        if state.active is None or service not in state.active.services:
            raise ProviderNotFoundError(f"service {service!r} not deployed")
        return iter([f"[fake {service}] log line"])

    def exec(
        self,
        ctx: DeployContext,
        service: str,
        command: list[str],
        interactive: bool = False,
    ) -> ExecResult:
        state = self._state(ctx)
        if state.active is None or service not in state.active.services:
            raise ProviderNotFoundError(f"service {service!r} not deployed")
        if command and command[0] == "echo":
            return ExecResult(
                exit_code=0,
                stdout=" ".join(command[1:]) + "\n",
                stderr="",
            )
        return ExecResult(exit_code=0, stdout="", stderr="")

    def rollback(
        self,
        ctx: DeployContext,
        to_revision: Optional[str] = None,
    ) -> DeployResult:
        state = self._state(ctx)
        if len(state.revisions) < 2:
            raise ProviderError("nothing to roll back to")
        if to_revision:
            target = next(
                (r for r in state.revisions if r.revision_id == to_revision),
                None,
            )
            if target is None:
                raise ProviderNotFoundError(f"revision {to_revision} not found")
        else:
            target = state.revisions[-2]
        clone = _Revision(
            revision_id=_next_revision_id(ctx.project),
            config_hash=target.config_hash,
            services={k: dict(v) for k, v in target.services.items()},
        )
        state.revisions.append(clone)
        state.active = clone
        return DeployResult(
            revision_id=clone.revision_id,
            services=list(clone.services.keys()),
            duration_s=0.01,
        )

    def destroy(self, ctx: DeployContext) -> None:
        self._projects.pop(ctx.project, None)

    def emit_terraform(self, ctx: DeployContext, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "# FakeProvider: placeholder terraform module.",
            "# Not apply-able against real infra. Real providers emit real HCL.",
            f'# project = "{ctx.project}"',
        ]
        for name, spec in sorted(ctx.services.items()):
            lines.append(
                f'# service: {name} cpu={spec.cpu} mem={spec.memory} replicas={spec.replicas}'
            )
        (out_dir / "main.tf").write_text("\n".join(lines) + "\n")
        return out_dir


register("fake", FakeProvider)
