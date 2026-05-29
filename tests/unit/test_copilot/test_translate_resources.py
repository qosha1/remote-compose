"""Translate Copilot service resource fields → rc.yml v2 overrides.

Fields covered: cpu, memory, count, exec.
"""

from __future__ import annotations

from pathlib import Path

from remote_compose.copilot.discover import CopilotService
from remote_compose.copilot.translate import translate_resources


def _svc(raw: dict) -> CopilotService:
    return CopilotService(
        name=raw.get("name", "x"),
        type=raw.get("type", "Backend Service"),
        manifest_path=Path("/dev/null"),
        raw=raw,
    )


class TestCpuMemory:
    def test_cpu_and_memory_carried_through(self):
        out, _ = translate_resources(
            _svc(
                {
                    "name": "api",
                    "cpu": 1024,
                    "memory": 2048,
                }
            )
        )
        assert out["cpu"] == 1024
        assert out["memory"] == 2048

    def test_string_values_coerced_to_int(self):
        # Copilot tolerates string ints in some contexts.
        out, _ = translate_resources(
            _svc(
                {
                    "name": "api",
                    "cpu": "512",
                    "memory": "1024",
                }
            )
        )
        assert out["cpu"] == 512
        assert out["memory"] == 1024

    def test_missing_cpu_memory_omitted_so_provider_defaults_apply(self):
        out, _ = translate_resources(_svc({"name": "api"}))
        assert "cpu" not in out
        assert "memory" not in out


class TestCount:
    def test_count_int_becomes_replicas(self):
        out, _ = translate_resources(_svc({"name": "api", "count": 3}))
        assert out["replicas"] == 3

    def test_count_zero_becomes_one_with_warning(self):
        # Copilot's count=0 means "scale-to-zero" which ECS doesn't
        # natively do at the service level. Treat as 1 + warn.
        out, warnings = translate_resources(_svc({"name": "api", "count": 0}))
        assert out["replicas"] == 1
        assert any("scale-to-zero" in w.message.lower() for w in warnings)

    def test_count_dict_advanced_scaling_warns(self):
        # Copilot count: { range: 1-10, cpu_percentage: 70 } is autoscaling.
        # Not yet supported by our provider; emit replicas=range_min + warn.
        out, warnings = translate_resources(
            _svc(
                {
                    "name": "api",
                    "count": {"range": "2-10", "cpu_percentage": 70},
                }
            )
        )
        assert out["replicas"] == 2
        assert any("autoscaling" in w.message.lower() for w in warnings)

    def test_count_missing_omitted(self):
        out, _ = translate_resources(_svc({"name": "api"}))
        assert "replicas" not in out


class TestExec:
    def test_exec_true_no_change(self):
        # Provider always sets enable_execute_command=true on ECS
        # services. exec: true on Copilot is the default we already
        # honor — emit nothing extra.
        out, warnings = translate_resources(_svc({"name": "api", "exec": True}))
        assert "exec" not in out
        assert warnings == []

    def test_exec_false_warns_because_provider_always_enables_it(self):
        # User explicitly disabled exec on Copilot. Our provider
        # currently always enables it. Warn so the user can decide
        # whether that matters.
        out, warnings = translate_resources(_svc({"name": "api", "exec": False}))
        assert any("exec" in w.message.lower() for w in warnings)
