"""m6a shapes (startsim-h2x8y.4): x86 AMD, same trunk caps as m6i, ~10% cheaper."""

from __future__ import annotations

import pytest

from remote_compose.provider.ecs.autosize import KNOWN_INSTANCE_SHAPES


@pytest.mark.parametrize(
    "itype, vcpu, mem, enis, trunk",
    [
        ("m6a.large", 2, 8, 3, 10),
        ("m6a.xlarge", 4, 16, 4, 20),
        ("m6a.2xlarge", 8, 32, 4, 40),
        ("m6a.8xlarge", 32, 128, 8, 90),
    ],
)
def test_m6a_rows_match_aws(itype, vcpu, mem, enis, trunk):
    s = KNOWN_INSTANCE_SHAPES[itype]
    assert (s.vcpu, s.memory_gib, s.max_enis, s.trunked_task_limit) == (
        vcpu,
        mem,
        enis,
        trunk,
    )


def test_m6a_matches_m6i_size_for_size():
    for size in ("large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge"):
        a, i = (
            KNOWN_INSTANCE_SHAPES[f"m6a.{size}"],
            KNOWN_INSTANCE_SHAPES[f"m6i.{size}"],
        )
        assert (a.vcpu, a.memory_gib, a.max_enis, a.trunked_task_limit) == (
            i.vcpu,
            i.memory_gib,
            i.max_enis,
            i.trunked_task_limit,
        )
