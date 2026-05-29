"""rc-td9: rc warns when cwd contains multiple rc*.yml configs and no
-c flag was passed.

Real-world repro 2026-04-30: sentinal had rc.yml (us-west-2 stale) +
rc.core.yml (us-west-1 active). `rc up` (no -c) silently picked rc.yml
and started creating resources against a non-existent VPC. The warning
makes the choice explicit.
"""

from __future__ import annotations


from remote_compose.cli import _warn_on_rc_yml_ambiguity


class TestWarnOnRcYmlAmbiguity:
    def test_no_warning_when_only_rc_yml(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "rc.yml").write_text("version: 2\n")
        monkeypatch.chdir(tmp_path)
        _warn_on_rc_yml_ambiguity()
        assert capsys.readouterr().err == ""

    def test_no_warning_when_no_configs(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _warn_on_rc_yml_ambiguity()
        assert capsys.readouterr().err == ""

    def test_warns_when_two_configs_exist(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "rc.yml").write_text("version: 2\n")
        (tmp_path / "rc.core.yml").write_text("version: 2\n")
        monkeypatch.chdir(tmp_path)
        _warn_on_rc_yml_ambiguity()
        err = capsys.readouterr().err
        assert "Multiple rc configs" in err
        assert "rc.yml" in err
        assert "rc.core.yml" in err

    def test_warns_lists_all_three_configs(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "rc.yml").write_text("version: 2\n")
        (tmp_path / "rc.dev.yml").write_text("version: 2\n")
        (tmp_path / "rc.prod.yml").write_text("version: 2\n")
        monkeypatch.chdir(tmp_path)
        _warn_on_rc_yml_ambiguity()
        err = capsys.readouterr().err
        for name in ("rc.yml", "rc.dev.yml", "rc.prod.yml"):
            assert name in err

    def test_no_warning_when_only_dot_variant_no_main(
        self, tmp_path, monkeypatch, capsys
    ):
        # Only rc.core.yml, no rc.yml — single candidate → no ambiguity.
        (tmp_path / "rc.core.yml").write_text("version: 2\n")
        monkeypatch.chdir(tmp_path)
        _warn_on_rc_yml_ambiguity()
        assert capsys.readouterr().err == ""
