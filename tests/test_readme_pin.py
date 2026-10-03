"""README install-pin auto-sync (see readme_pin.py)."""
import textwrap


def _readme_body():
    return (
        "# Hermes Mobile Plugin\n"
        "pip install git+https://github.com/tawaresachin/hermes-mobile-plugin@v0.0.12\n"
    )


class TestReadmePinSync:
    def _readme(self, tmp_path, body):
        p = tmp_path / "README.md"
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return p

    def test_rewrites_stale_pin_to_new_tag(self, tmp_path):
        from hermes_mobile_plugin import readme_pin

        readme = self._readme(
            tmp_path,
            """\
            # Hermes Mobile Plugin
            pip install git+https://github.com/tawaresachin/hermes-mobile-plugin@v0.0.12
            """,
        )
        report = readme_pin.sync_readme_pin(readme, "0.0.20")
        assert report["updated"] is True
        assert report["pins"] == 1
        assert "hermes-mobile-plugin@v0.0.20" in readme.read_text(encoding="utf-8")
        assert "@v0.0.12" not in readme.read_text(encoding="utf-8")

    def test_is_idempotent_when_already_at_tag(self, tmp_path):
        from hermes_mobile_plugin import readme_pin

        readme = self._readme(
            tmp_path,
            """\
            pip install git+https://github.com/tawaresachin/hermes-mobile-plugin@v0.0.20
            """,
        )
        before = readme.read_text(encoding="utf-8")
        report = readme_pin.sync_readme_pin(readme, "v0.0.20")
        assert report["updated"] is False
        assert readme.read_text(encoding="utf-8") == before

    def test_accepts_tag_with_or_without_v_prefix(self, tmp_path):
        from hermes_mobile_plugin import readme_pin

        for tag in ("v0.0.20", "0.0.20"):
            readme = self._readme(
                tmp_path,
                """\
                pip install git+https://github.com/tawaresachin/hermes-mobile-plugin@v0.0.1
                """,
            )
            report = readme_pin.sync_readme_pin(readme, tag)
            assert report["updated"] is True
            assert "hermes-mobile-plugin@v0.0.20" in readme.read_text(encoding="utf-8")

    def test_updates_every_pin_in_the_file(self, tmp_path):
        from hermes_mobile_plugin import readme_pin

        readme = self._readme(
            tmp_path,
            """\
            pip install git+https://github.com/tawaresachin/hermes-mobile-plugin@v0.0.9
            install.bat -> hermes-mobile-plugin@v0.0.9 on Windows
            """,
        )
        report = readme_pin.sync_readme_pin(readme, "v0.0.20")
        assert report["pins"] == 2
        assert readme.read_text(encoding="utf-8").count("@v0.0.20") == 2
        assert "@v0.0.9" not in readme.read_text(encoding="utf-8")

    def test_missing_readme_reports_instead_of_raising(self, tmp_path):
        from hermes_mobile_plugin import readme_pin

        report = readme_pin.sync_readme_pin(tmp_path / "no-such.md", "v0.0.20")
        assert report["error"] == "README not found"
        assert report["updated"] is False

    def test_reads_no_version_from_unrelated_lines(self, tmp_path):
        from hermes_mobile_plugin import readme_pin

        readme = self._readme(
            tmp_path,
            """\
            # Hermes Mobile Plugin
            version in plugin.yaml: 0.0.20
            something @v0.0.12
            """,
        )
        report = readme_pin.sync_readme_pin(readme, "v0.0.20")
        assert report["pins"] == 0
        assert report["updated"] is False


class TestReadmePinCli:
    def test_main_syncs_readme_in_cwd(self, tmp_path, monkeypatch, capsys):
        from hermes_mobile_plugin import readme_pin

        readme = tmp_path / "README.md"
        readme.write_text(_readme_body(), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert readme_pin.main(["v0.0.20"]) == 0
        assert "hermes-mobile-plugin@v0.0.20" in readme.read_text(encoding="utf-8")

    def test_main_requires_exactly_one_argument(self, capsys):
        from hermes_mobile_plugin import readme_pin

        assert readme_pin.main([]) == 2
        assert readme_pin.main(["a", "b"]) == 2
