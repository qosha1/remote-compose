"""rc.yml v2 ``network:`` / ``repositories:`` schema + cross-reference validation."""

from __future__ import annotations

import pytest

from remote_compose.config.v2_schema import ConfigError, parse

pytestmark = pytest.mark.unit


def _cfg(**extra):
    base = {
        "version": 2,
        "project": "p",
        "compose_file": "docker-compose.yml",
        "provider": "ecs",
    }
    base.update(extra)
    return base


def _net(network=None, **extra):
    return parse(_cfg(network=network or {}, **extra))


class TestBackCompat:
    def test_absent_blocks_default_to_empty(self):
        cfg = parse(_cfg())
        assert cfg.network.is_empty()
        assert cfg.repositories == {}

    def test_empty_blocks_are_accepted(self):
        cfg = parse(_cfg(network={}, repositories={}))
        assert cfg.network.is_empty()


class TestSecurityGroups:
    def test_declares_ingress_and_egress(self):
        cfg = _net(
            {
                "security_groups": {
                    "web": {},
                    "runners": {
                        "description": "d",
                        "ingress": [{"from": "sg:web", "ports": [8000]}],
                        "egress": [{"to": "cidr:10.0.0.0/16", "ports": ["8000-8100"]}],
                    },
                }
            }
        )
        sg = cfg.network.security_groups["runners"]
        assert sg.description == "d"
        assert sg.ingress[0].ref.kind == "sg"
        assert sg.ingress[0].ref.value == "web"
        assert (sg.ingress[0].ports[0].from_port, sg.ingress[0].ports[0].to_port) == (
            8000,
            8000,
        )
        assert (sg.egress[0].ports[0].from_port, sg.egress[0].ports[0].to_port) == (
            8000,
            8100,
        )

    def test_no_rules_is_valid_and_means_deny(self):
        cfg = _net({"security_groups": {"locked": {}}})
        sg = cfg.network.security_groups["locked"]
        assert sg.ingress == [] and sg.egress == []

    @pytest.mark.parametrize(
        "ref",
        ["nope:x", "sg", "sg:", "cidr:not-a-cidr", "cidr:10.0.0.0/99", ""],
    )
    def test_malformed_references_are_rejected(self, ref):
        with pytest.raises(ConfigError):
            _net({"security_groups": {"a": {"ingress": [{"from": ref}]}}})

    def test_bare_alb_and_self_are_accepted(self):
        cfg = _net(
            {
                "security_groups": {
                    "a": {
                        "ingress": [{"from": "self"}],
                        "egress": [{"to": "cidr:0.0.0.0/0"}],
                    }
                }
            },
            services={"w": {"public": True, "port": 80}},
        )
        assert cfg.network.security_groups["a"].ingress[0].ref.kind == "self"

    def test_dangling_sg_reference_is_rejected(self):
        with pytest.raises(ConfigError, match="does not name a declared security"):
            _net({"security_groups": {"a": {"ingress": [{"from": "sg:ghost"}]}}})

    def test_self_reference_by_name_points_at_the_keyword(self):
        with pytest.raises(ConfigError, match="use the bare 'self' keyword"):
            _net({"security_groups": {"a": {"ingress": [{"from": "sg:a"}]}}})

    def test_ingress_from_an_endpoint_is_rejected(self):
        with pytest.raises(ConfigError, match="not a valid ingress source"):
            _net({"security_groups": {"a": {"ingress": [{"from": "endpoint:x"}]}}})

    def test_direction_keyword_is_enforced(self):
        with pytest.raises(ConfigError, match="ingress rules use 'from'"):
            _net({"security_groups": {"a": {"ingress": [{"to": "cidr:0.0.0.0/0"}]}}})
        with pytest.raises(ConfigError, match="egress rules use 'to'"):
            _net({"security_groups": {"a": {"egress": [{"from": "cidr:0.0.0.0/0"}]}}})

    def test_protocol_all_rejects_ports(self):
        with pytest.raises(ConfigError, match="cannot carry a port list"):
            _net(
                {
                    "security_groups": {
                        "a": {
                            "egress": [
                                {
                                    "to": "cidr:0.0.0.0/0",
                                    "protocol": "all",
                                    "ports": [53],
                                }
                            ]
                        }
                    }
                }
            )

    def test_unknown_rule_key_is_rejected(self):
        with pytest.raises(ConfigError, match="unknown egress rule key"):
            _net(
                {
                    "security_groups": {
                        "a": {"egress": [{"to": "cidr:0.0.0.0/0", "prt": 80}]}
                    }
                }
            )


class TestSubnetGroups:
    def test_private_defaults_to_no_egress(self):
        cfg = _net({"subnets": {"iso": {}}})
        group = cfg.network.subnets["iso"]
        assert group.public is False and group.egress == "none" and group.count == 2

    def test_public_defaults_to_igw(self):
        cfg = _net({"subnets": {"pub": {"public": True}}})
        assert cfg.network.subnets["pub"].egress == "igw"

    def test_public_with_explicit_other_egress_is_rejected(self):
        with pytest.raises(ConfigError, match="always egress via the internet"):
            _net({"subnets": {"pub": {"public": True, "egress": "nat"}}})

    def test_private_with_igw_egress_is_rejected(self):
        with pytest.raises(ConfigError, match="requires public: true"):
            _net({"subnets": {"p": {"public": False, "egress": "igw"}}})

    @pytest.mark.parametrize("mode", ["endpoints", "nat", "none"])
    def test_private_egress_modes(self, mode):
        cfg = _net({"subnets": {"p": {"egress": mode}}})
        assert cfg.network.subnets["p"].egress == mode

    def test_unknown_egress_mode_is_rejected(self):
        with pytest.raises(ConfigError, match="egress must be one of"):
            _net({"subnets": {"p": {"egress": "carrier-pigeon"}}})

    def test_cidr_offset_colliding_with_builtins_is_rejected(self):
        # 10-11 belong to rc's built-in private subnets.
        with pytest.raises(ConfigError, match="belong to rc's built-in"):
            _net({"subnets": {"p": {"cidr_offset": 10}}})

    def test_explicit_cidrs_must_match_count(self):
        with pytest.raises(ConfigError, match="one CIDR per subnet"):
            _net({"subnets": {"p": {"count": 2, "cidrs": ["10.0.9.0/24"]}}})

    def test_cidrs_and_offset_are_mutually_exclusive(self):
        with pytest.raises(ConfigError, match="mutually exclusive"):
            _net(
                {
                    "subnets": {
                        "p": {
                            "count": 1,
                            "cidrs": ["10.0.9.0/24"],
                            "cidr_offset": 30,
                        }
                    }
                }
            )


class TestEndpoints:
    def _with_ecr(self, **ep):
        spec = {"services": ["ecr.api"], "subnets": ["p"]}
        spec.update(ep)
        return {
            "security_groups": {"a": {"egress": [{"to": "endpoint:e"}]}},
            "subnets": {"p": {}},
            "endpoints": {"e": spec},
        }

    def test_interface_type_is_inferred(self):
        cfg = _net(self._with_ecr())
        assert cfg.network.endpoints["e"].resolved_type == "Interface"

    def test_gateway_type_is_inferred_for_s3(self):
        cfg = _net(self._with_ecr(services=["s3"]))
        assert cfg.network.endpoints["e"].resolved_type == "Gateway"

    def test_mixing_gateway_and_interface_services_is_rejected(self):
        with pytest.raises(ConfigError, match="cannot mix gateway service"):
            _net(self._with_ecr(services=["s3", "logs"]))

    def test_subnets_are_required(self):
        with pytest.raises(ConfigError, match="subnets is required"):
            _net(self._with_ecr(subnets=[]))

    def test_dangling_subnet_reference_is_rejected(self):
        with pytest.raises(ConfigError, match="does not name a declared subnet"):
            _net(self._with_ecr(subnets=["ghost"]))

    def test_unreachable_interface_endpoint_is_rejected(self):
        """An endpoint nothing egresses to derives an empty ingress — paid ENIs
        serving no traffic. Always a mistake, so refuse it."""
        with pytest.raises(ConfigError, match="are unreachable"):
            _net(
                {
                    "subnets": {"p": {}},
                    "endpoints": {"e": {"services": ["logs"], "subnets": ["p"]}},
                }
            )

    def test_unreferenced_gateway_endpoint_is_allowed(self):
        """A gateway endpoint works purely by route-table entry — it needs no
        security group and therefore no inbound grant."""
        cfg = _net(
            {
                "subnets": {"p": {}},
                "endpoints": {"s3": {"services": ["s3"], "subnets": ["p"]}},
            }
        )
        assert cfg.network.endpoints["s3"].resolved_type == "Gateway"


class TestServicePlacement:
    def test_service_can_replace_its_security_groups(self):
        cfg = parse(
            _cfg(
                network={"security_groups": {"runners": {}}, "subnets": {"p": {}}},
                services={"w": {"security_groups": ["runners"], "subnets": "p"}},
            )
        )
        assert cfg.services["w"].security_groups == ["runners"]
        assert cfg.services["w"].subnets == "p"

    def test_placement_defaults_to_none(self):
        cfg = parse(_cfg(services={"w": {}}))
        assert cfg.services["w"].security_groups is None
        assert cfg.services["w"].subnets is None

    def test_dangling_service_sg_reference_is_rejected(self):
        with pytest.raises(ConfigError, match="does not name a declared"):
            parse(_cfg(services={"w": {"security_groups": ["ghost"]}}))

    def test_dangling_service_subnet_reference_is_rejected(self):
        with pytest.raises(ConfigError, match="does not name a declared"):
            parse(_cfg(services={"w": {"subnets": "ghost"}}))

    def test_empty_security_groups_list_is_rejected(self):
        with pytest.raises(ConfigError, match="security_groups is empty"):
            parse(_cfg(services={"w": {"security_groups": []}}))

    def test_public_service_replacing_sgs_must_readmit_the_alb(self):
        with pytest.raises(ConfigError, match="none of them admit the load balancer"):
            parse(
                _cfg(
                    network={"security_groups": {"tight": {}}},
                    services={
                        "w": {
                            "public": True,
                            "port": 8000,
                            "security_groups": ["tight"],
                        }
                    },
                )
            )

    def test_public_service_with_an_alb_ingress_rule_is_accepted(self):
        cfg = parse(
            _cfg(
                network={
                    "security_groups": {
                        "tight": {"ingress": [{"from": "alb", "ports": [8000]}]}
                    }
                },
                services={
                    "w": {"public": True, "port": 8000, "security_groups": ["tight"]}
                },
            )
        )
        assert cfg.services["w"].security_groups == ["tight"]

    def test_service_ref_is_not_resolved_at_parse_time(self):
        """A 'service:<name>' target may live only in docker-compose.yml, so
        parse() cannot reject it — the provider re-validates with the merged
        service set."""
        cfg = parse(
            _cfg(
                network={
                    "security_groups": {
                        "a": {"ingress": [{"from": "service:only-in-compose"}]}
                    }
                }
            )
        )
        assert (
            cfg.network.security_groups["a"].ingress[0].ref.value == "only-in-compose"
        )


class TestRepositories:
    def test_declares_a_mirror_repo(self):
        cfg = parse(
            _cfg(
                repositories={
                    "db-sidecar": {
                        "mirror": "postgres:16-alpine",
                        "expire_untagged_days": 30,
                    }
                }
            )
        )
        repo = cfg.repositories["db-sidecar"]
        assert repo.mirror == "postgres:16-alpine"
        assert repo.expire_untagged_days == 30
        assert repo.mutable is True and repo.scan_on_push is True

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ConfigError, match="unknown key"):
            parse(_cfg(repositories={"r": {"tags": ["x"]}}))

    def test_bad_name_is_rejected(self):
        with pytest.raises(ConfigError, match="is invalid"):
            parse(_cfg(repositories={"Not_Valid": {}}))


class TestHclInjectionRegressions:
    """Free text that rc interpolates into generated HCL.

    Validated at parse time rather than escaped downstream: every rejected
    character is one AWS refuses anyway, so nothing legitimate is lost.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            'ev"il',  # closes the HCL string -> unparseable .tf
            "back\\slash",  # escape sequence
            "two\nlines",  # escapes the line entirely
            "x" * 256,  # over AWS's 255-char limit
        ],
    )
    def test_unsafe_group_descriptions_are_rejected(self, bad):
        with pytest.raises(ConfigError, match="description"):
            _net({"security_groups": {"a": {"description": bad}}})

    def test_unsafe_rule_descriptions_are_rejected(self):
        with pytest.raises(ConfigError, match="description"):
            _net(
                {
                    "security_groups": {
                        "a": {
                            "ingress": [
                                {"from": "cidr:10.0.0.0/16", "description": 'ev"il'}
                            ]
                        }
                    }
                }
            )

    def test_ordinary_descriptions_are_accepted(self):
        cfg = _net(
            {
                "security_groups": {
                    "a": {"description": "Ephemeral runners (tier: web) - no ingress."}
                }
            }
        )
        assert cfg.network.security_groups["a"].description.startswith("Ephemeral")

    def test_mirror_cannot_inject_terraform_resources(self):
        """`mirror` renders into a comment; a newline would close it and let
        the remainder parse as HCL -- from a file that travels in the repo."""
        payload = 'postgres:16"\nresource "aws_iam_user" "pwn" {\n  name = "pwn"\n}\n#'
        with pytest.raises(ConfigError, match="not a valid image reference"):
            parse(_cfg(repositories={"r": {"mirror": payload}}))

    @pytest.mark.parametrize(
        "ref",
        [
            "postgres:16-alpine",
            "docker.io/library/postgres:16",
            "registry.example.com/team/img@sha256:" + "a" * 64,
        ],
    )
    def test_real_image_references_are_accepted(self, ref):
        cfg = parse(_cfg(repositories={"r": {"mirror": ref}}))
        assert cfg.repositories["r"].mirror == ref


class TestProtocolRegressions:
    def test_icmpv6_is_rejected(self):
        """rc rejects IPv6 CIDRs outright, so an icmpv6 rule could only pair
        with an IPv4 source -- which AWS refuses at apply."""
        with pytest.raises(ConfigError, match="protocol"):
            _net(
                {
                    "security_groups": {
                        "a": {
                            "ingress": [
                                {"from": "cidr:10.0.0.0/16", "protocol": "icmpv6"}
                            ]
                        }
                    }
                }
            )
