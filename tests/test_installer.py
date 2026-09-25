import argparse
import os
from unittest.mock import patch
import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy.install import configure_reference_access, MARKER, check_nginx_domain, nginx_conf, publish_nginx, unit_files, validate_domain, wait_for_site


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.site = self.root / "conf.d/pcms-lite.conf"
        self.other = self.root / "conf.d/existing.conf"
        self.other.parent.mkdir()
        self.other.write_text("server { listen 80; server_name existing.example.org; }\n")
        self.original = self.other.read_bytes()
        self.calls = []
        self.fail_validation = False
        self.fail_reload = False
        self.include_site = True

    def tearDown(self):
        self.assertEqual(self.other.read_bytes(), self.original)
        self.tmp.cleanup()

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if args == ("nginx", "-T"):
            result = f"# configuration file {self.other}:\n" + self.other.read_text()
            if self.site.exists() and self.include_site:
                result += f"\n# configuration file {self.site}:\n" + self.site.read_text()
            return result
        if args == ("nginx", "-t") and self.fail_validation:
            self.fail_validation = False
            raise subprocess.CalledProcessError(1, args)
        if args == ("systemctl", "reload", "nginx") and self.fail_reload:
            self.fail_reload = False
            raise subprocess.CalledProcessError(1, args)
        return ""

    def test_reference_access_preserves_profiles_and_private_credentials(self):
        conf = self.root / 'config'
        conf.mkdir(mode=0o700)
        secret = conf / 'admin-credentials'
        secret.write_text('private-test-password')
        secret.chmod(0o600)
        with patch('deploy.install.os.chown') as chown:
            configure_reference_access(conf, os.getgid())
            profile = conf / 'reference-languages.json'
            self.assertEqual(profile.read_text(), '{}\n')
            profile.write_text('{"custom": {"source": "main.py"}}')
            configure_reference_access(conf, os.getgid())
            self.assertIn('custom', profile.read_text())
            chown.assert_any_call(conf, 0, os.getgid())
            chown.assert_any_call(profile, 0, os.getgid())
        self.assertEqual(conf.stat().st_mode & 0o777, 0o710)
        self.assertEqual(profile.stat().st_mode & 0o777, 0o640)
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(secret.read_text(), 'private-test-password')

    def publish(self):
        return publish_nginx(nginx_conf("judge.example.org"), "judge.example.org", self.site, self.command, self.root / "backups")

    def test_adds_only_own_site_and_validates_before_reload(self):
        self.publish()
        self.assertTrue(self.site.read_text().startswith(MARKER))
        self.assertLess(self.calls.index(("nginx", "-t")), self.calls.index(("systemctl", "reload", "nginx")))
        self.assertNotIn("default_server", self.site.read_text())

    def test_unmanaged_site_is_untouched(self):
        self.site.write_text("# hand written\nserver {}\n")
        before = self.site.read_bytes()
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.site.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_failed_nginx_test_removes_new_file(self):
        self.fail_validation = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertFalse(self.site.exists())

    def test_failed_reload_restores_existing_site_and_backup(self):
        old = MARKER + "\n# previous settings\nserver { listen 80; server_name judge.example.org; }\n"
        self.site.write_text(old)
        self.fail_reload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual(self.site.read_text(), old)
        self.assertEqual((self.root / "backups/pcms-lite.conf.bak").read_text(), old)

    def test_missing_include_does_not_silently_succeed(self):
        self.include_site = False
        with self.assertRaisesRegex(RuntimeError, "не подключает"):
            self.publish()
        self.assertFalse(self.site.exists())

    def test_domain_conflict_in_multiline_directive(self):
        dump = "# configuration file /etc/nginx/sites-enabled/old:\nserver {\n server_name\n    other.org\n    judge.example.org;\n}\n"
        with self.assertRaises(RuntimeError):
            check_nginx_domain(dump, "judge.example.org")

    def test_domain_and_config_injection_rejected(self):
        for value in ["x;return 200", "a b", "$(id)", "https://example.org", "../x", "_", "x\n}", "*.example.org"]:
            with self.assertRaises(argparse.ArgumentTypeError):
                validate_domain(value)
        self.assertEqual(validate_domain("Judge.Example.org."), "judge.example.org")
        self.assertEqual(validate_domain("192.0.2.10"), "192.0.2.10")

    def test_no_edit_to_nginx_main_or_external_tls_config(self):
        self.publish()
        self.assertEqual(set(p.name for p in self.other.parent.iterdir()), {"existing.conf", "pcms-lite.conf"})
        tls = nginx_conf("judge.example.org", True)
        self.assertIn("/etc/letsencrypt/live/pcms-lite/fullchain.pem", tls)
        self.assertIn("/.well-known/acme-challenge/", tls)
        self.assertNotIn("default_server", tls)

    def test_readiness_waits_for_old_site_and_transient_502(self):
        replies = iter(["Welcome to nginx", subprocess.CalledProcessError(22, "curl", stderr="HTTP 502"), "Система проверки решений"])
        sleeps = []
        def probe(*args, **kwargs):
            self.assertIn("--noproxy", args)
            self.assertIn("judge.example.org:80:127.0.0.1", args)
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value
        wait_for_site("judge.example.org", command=probe, sleep=sleeps.append, attempts=3)
        self.assertEqual(sleeps, [1, 1])

    def test_readiness_reports_last_curl_error(self):
        def probe(*args, **kwargs):
            raise subprocess.CalledProcessError(7, "curl", stderr="Connection refused")
        with self.assertRaisesRegex(RuntimeError, "Connection refused"):
            wait_for_site("judge.example.org", command=probe, sleep=lambda _: None, attempts=2)

    def test_https_readiness_rejects_wrong_site(self):
        def probe(*args, **kwargs):
            self.assertIn("judge.example.org:443:127.0.0.1", args)
            return "some other login page"
        with self.assertRaisesRegex(RuntimeError, "другого сайта"):
            wait_for_site("judge.example.org", tls=True, command=probe, sleep=lambda _: None, attempts=2)

    def test_services_use_socket_and_dedicated_sandbox(self):
        units = unit_files(True, 1)
        self.assertIn("unix:/run/pcms-lite/web.sock", units["pcms-web.service"])
        self.assertIn("PCMS_HTTPS=1", units["pcms-web.service"])
        self.assertIn("PCMS_ISOLATE=", units["pcms-worker@.service"])
        self.assertIn("Requires=pcms-isolate.service var-lib-pcmsboxes.mount", units["pcms-worker@.service"])
        self.assertNotIn("NoNewPrivileges", units["pcms-worker@.service"])


if __name__ == "__main__":
    unittest.main()
