"""Unit tests for remote_compose.terraform.backend."""

from __future__ import annotations

import pytest

from remote_compose.terraform.backend import (
    UnsupportedBackendError,
    render_backend_block,
)


class TestRenderBackendBlock:
    def test_local_backend_default(self):
        block = render_backend_block({"type": "local"})
        assert 'backend "local"' in block
        assert block.count("{") == 2
        assert block.count("}") == 2

    def test_s3_backend_renders_all_required(self):
        block = render_backend_block({
            "type": "s3",
            "bucket": "myapp-tf-state",
            "key": "myapp/ecs.tfstate",
            "region": "us-west-2",
            "dynamodb_table": "tf-locks",
        })
        assert 'backend "s3"' in block
        assert 'bucket' in block and '"myapp-tf-state"' in block
        assert 'key' in block and '"myapp/ecs.tfstate"' in block
        assert 'dynamodb_table' in block and '"tf-locks"' in block

    def test_gcs_backend(self):
        block = render_backend_block({
            "type": "gcs",
            "bucket": "myapp-state",
            "prefix": "myapp",
        })
        assert 'backend "gcs"' in block
        assert '"myapp-state"' in block

    def test_unknown_backend_rejected(self):
        with pytest.raises(UnsupportedBackendError, match="bogus"):
            render_backend_block({"type": "bogus"})

    def test_none_values_omitted(self):
        block = render_backend_block({
            "type": "s3",
            "bucket": "x",
            "key": "y",
            "region": None,
        })
        assert "region" not in block

    def test_string_values_escape_quotes(self):
        block = render_backend_block({
            "type": "local",
            "path": 'weird"path',
        })
        assert 'weird\\"path' in block

    def test_boolean_values_render_as_hcl_bools(self):
        block = render_backend_block({
            "type": "s3",
            "bucket": "x",
            "key": "y",
            "encrypt": True,
        })
        assert "encrypt" in block
        assert "= true" in block

    def test_extra_fields_passthrough(self):
        block = render_backend_block({
            "type": "s3",
            "bucket": "x",
            "key": "y",
            "extra": {"role_arn": "arn:aws:iam::1:role/tf"},
        })
        assert "role_arn" in block
        assert "arn:aws:iam::1:role/tf" in block
