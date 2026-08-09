"""rc.yml v2 ``iam_roles:`` schema + service-reference validation."""

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


def _roles(roles=None, services=None):
    return parse(_cfg(iam_roles=roles or {}, services=services or {}))


class TestBackCompat:
    def test_absent_block_defaults_to_empty(self):
        cfg = parse(_cfg())
        assert cfg.iam_roles == {}

    def test_empty_block_is_accepted(self):
        assert _roles({}).iam_roles == {}

    def test_service_without_iam_role_keeps_none(self):
        cfg = parse(_cfg(services={"web": {"cpu": 256}}))
        assert cfg.services["web"].iam_role is None

    def test_provider_config_ecs_iam_is_untouched(self):
        """The shared-role grant block keeps its own meaning and shape."""
        cfg = parse(
            _cfg(
                provider_config={
                    "ecs": {
                        "iam": {
                            "managed_policies": ["arn:aws:iam::aws:policy/ReadOnly"],
                            "statements": [
                                {"actions": ["s3:GetObject"], "resources": ["*"]}
                            ],
                        }
                    }
                }
            )
        )
        assert cfg.provider_config["ecs"]["iam"]["managed_policies"] == [
            "arn:aws:iam::aws:policy/ReadOnly"
        ]


class TestDeclaringRoles:
    def test_full_role_round_trips(self):
        cfg = _roles(
            {
                "media-writer": {
                    "description": "S3 media write",
                    "managed_policies": [
                        "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess"
                    ],
                    "statements": [
                        {
                            "sid": "WriteMedia",
                            "actions": ["s3:PutObject"],
                            "resources": ["arn:aws:s3:::b/*"],
                            "condition": {"Bool": {"aws:SecureTransport": "true"}},
                        }
                    ],
                    "tags": {"tier": "web", "cost": 42},
                }
            }
        )
        role = cfg.iam_roles["media-writer"]
        assert role.description == "S3 media write"
        assert role.managed_policies == [
            "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess"
        ]
        assert role.statements[0].sid == "WriteMedia"
        assert role.statements[0].condition == {"Bool": {"aws:SecureTransport": "true"}}
        # Scalars are coerced the way service env values are; AWS wants strings.
        assert role.tags == {"tier": "web", "cost": "42"}

    def test_empty_role_is_valid_and_means_no_grants(self):
        """Same contract as a declared SG with no rules: nothing, deliberately."""
        role = _roles({"locked-down": {}}).iam_roles["locked-down"]
        assert role.managed_policies == [] and role.statements == []
        assert role.policy_document() is None

    def test_scalar_managed_policy_is_wrapped(self):
        role = _roles(
            {"r": {"managed_policies": "arn:aws:iam::aws:policy/ReadOnlyAccess"}}
        ).iam_roles["r"]
        assert role.managed_policies == ["arn:aws:iam::aws:policy/ReadOnlyAccess"]

    def test_policy_document_generates_positional_sids(self):
        role = _roles(
            {
                "r": {
                    "statements": [
                        {"actions": ["s3:GetObject"], "resources": ["*"]},
                        {"sid": "Named", "actions": ["sqs:*"], "resources": ["*"]},
                    ]
                }
            }
        ).iam_roles["r"]
        doc = role.policy_document()
        assert [s["Sid"] for s in doc["Statement"]] == ["Grant0", "Named"]
        assert {s["Effect"] for s in doc["Statement"]} == {"Allow"}
        assert "Condition" not in doc["Statement"][0]


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"statements": [{"resources": ["*"]}]},
            {"statements": [{"actions": [], "resources": ["*"]}]},
            {"statements": [{"actions": ["s3:*"]}]},
            {"statements": [{"actions": ["s3:*"], "resources": []}]},
        ],
    )
    def test_statement_needs_actions_and_resources(self, body):
        with pytest.raises(ConfigError, match="actions|resources"):
            _roles({"r": body})

    def test_unknown_role_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown key"):
            _roles({"r": {"policies": []}})

    def test_unknown_statement_key_rejected(self):
        with pytest.raises(ConfigError, match="unknown statement key"):
            _roles(
                {
                    "r": {
                        "statements": [
                            {
                                "actions": ["s3:*"],
                                "resources": ["*"],
                                "effect": "Deny",
                            }
                        ]
                    }
                }
            )

    @pytest.mark.parametrize(
        "arn",
        [
            "AmazonS3FullAccess",
            "arn:aws:iam::aws:role/Something",
            "arn:aws:s3:::bucket",
        ],
    )
    def test_managed_policy_must_be_a_policy_arn(self, arn):
        with pytest.raises(ConfigError, match="policy ARN"):
            _roles({"r": {"managed_policies": [arn]}})

    def test_duplicate_managed_policy_rejected(self):
        arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
        with pytest.raises(ConfigError, match="twice"):
            _roles({"r": {"managed_policies": [arn, arn]}})

    def test_too_many_managed_policies_rejected(self):
        arns = [f"arn:aws:iam::aws:policy/P{i}" for i in range(21)]
        with pytest.raises(ConfigError, match="exceeds the AWS limit"):
            _roles({"r": {"managed_policies": arns}})

    @pytest.mark.parametrize("sid", ["with-hyphen", "with_underscore", "with space"])
    def test_non_alphanumeric_sid_rejected(self, sid):
        with pytest.raises(ConfigError, match="alphanumeric"):
            _roles(
                {
                    "r": {
                        "statements": [
                            {"sid": sid, "actions": ["s3:*"], "resources": ["*"]}
                        ]
                    }
                }
            )

    def test_duplicate_sid_rejected(self):
        with pytest.raises(ConfigError, match="duplicate sid"):
            _roles(
                {
                    "r": {
                        "statements": [
                            {"sid": "Same", "actions": ["s3:*"], "resources": ["*"]},
                            {"sid": "Same", "actions": ["sqs:*"], "resources": ["*"]},
                        ]
                    }
                }
            )

    def test_condition_must_be_a_mapping(self):
        with pytest.raises(ConfigError, match="condition must be a mapping"):
            _roles(
                {
                    "r": {
                        "statements": [
                            {
                                "actions": ["s3:*"],
                                "resources": ["*"],
                                "condition": ["nope"],
                            }
                        ]
                    }
                }
            )

    @pytest.mark.parametrize("name", ["Upper", "trailing-", "with_underscore", "-lead"])
    def test_role_name_grammar_matches_the_network_layer(self, name):
        with pytest.raises(ConfigError, match="is invalid"):
            _roles({name: {}})

    def test_statements_must_be_a_list(self):
        with pytest.raises(ConfigError, match="statements must be a list"):
            _roles({"r": {"statements": {"actions": ["s3:*"]}}})

    def test_nested_tag_value_rejected(self):
        with pytest.raises(ConfigError, match="flat mapping"):
            _roles({"r": {"tags": {"k": {"nested": 1}}}})


class TestServiceReferences:
    def test_service_can_name_a_declared_role(self):
        cfg = _roles({"r": {}}, services={"web": {"iam_role": "r"}})
        assert cfg.services["web"].iam_role == "r"

    def test_unknown_role_reference_is_rejected_by_name(self):
        with pytest.raises(ConfigError) as exc:
            _roles({"known": {}}, services={"web": {"iam_role": "typo"}})
        msg = str(exc.value)
        assert "'typo'" in msg and "known" in msg

    def test_reference_with_no_roles_block_names_the_empty_set(self):
        with pytest.raises(ConfigError, match="known: none"):
            parse(_cfg(services={"web": {"iam_role": "r"}}))

    def test_non_string_iam_role_rejected(self):
        with pytest.raises(ConfigError, match="iam_role must be a single name"):
            _roles({"r": {}}, services={"web": {"iam_role": ["r"]}})

    def test_several_services_may_share_one_role(self):
        """The reuse case that motivated a named block over inline grants."""
        cfg = _roles(
            {"tier": {}},
            services={"web": {"iam_role": "tier"}, "worker": {"iam_role": "tier"}},
        )
        assert cfg.services["web"].iam_role == cfg.services["worker"].iam_role

    def test_unreferenced_role_is_allowed(self):
        """It costs nothing and its ARN is exported for out-of-band consumers."""
        cfg = _roles({"for-a-lambda": {}}, services={"web": {}})
        assert "for-a-lambda" in cfg.iam_roles
