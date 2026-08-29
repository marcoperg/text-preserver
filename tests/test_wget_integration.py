from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import threading
import unittest

from text_preserver.preservation.capture import execute_capture, plan_capture
from text_preserver.config import load_config
from text_preserver.preservation.fixity import verify_capture


def wget_has_warc_support() -> bool:
    executable = shutil.which("wget")
    if executable is None:
        return False
    result = subprocess.run(
        [executable, "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0 and "--warc-file" in result.stdout


@unittest.skipUnless(wget_has_warc_support(), "GNU Wget with WARC support is required")
class WgetIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.requests: list[str] = []

        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.path)
                if self.path == "/robots.txt":
                    self._send(b"User-agent: *\nAllow: /\n", "text/plain")
                elif self.path == "/index.html":
                    self._send(
                        b'<html><head><link rel="stylesheet" href="/style.css"></head>'
                        b'<body><a href="/child.html">Child</a></body></html>',
                        "text/html",
                    )
                elif self.path == "/child.html":
                    self._send(b"<html><body>Preserved child</body></html>", "text/html")
                elif self.path == "/style.css":
                    self._send(b"body { color: black; }", "text/css")
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"http://localhost:{self.server.server_port}/redirect-target",
                    )
                    self.end_headers()
                elif self.path == "/redirect-target":
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_port}/second-target",
                    )
                    self.end_headers()
                elif self.path == "/single-redirect":
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"http://localhost:{self.server.server_port}/single-target",
                    )
                    self.end_headers()
                elif self.path == "/single-target":
                    self._send(b"reviewed redirect target", "text/plain")
                elif self.path == "/second-target":
                    self._send(b"second redirect followed", "text/plain")
                else:
                    self.send_error(404)

            def _send(self, content: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def write_config(
        self,
        seed_path: str,
        *,
        mirror: bool = True,
        reviewed_redirects: tuple[tuple[str, str], ...] = (),
    ) -> Path:
        port = self.server.server_port
        redirect_config = ""
        if reviewed_redirects:
            entries = ", ".join(
                f'{{ from = "{source}", to = "{target}" }}'
                for source, target in reviewed_redirects
            )
            redirect_config = f"reviewed_redirects = [{entries}]"
        source_capture = ""
        if not mirror:
            source_capture = """

[collections.sources.capture]
mirror = false
page_requisites = false
convert_links = false
adjust_extension = false
"""
        config = f"""
[project]
archive_root = "./archive"
operator = "Integration test"
contact = "mailto:test@example.org"
user_agent = "text-preserver-integration-test/1.0"

[defaults.capture]
wait = 0
random_wait = false
timeout = 5
tries = 1
quota = "5M"
warc_max_size = "1M"

[[collections]]
id = "local-fixture"
title = "Local Fixture"

[[collections.sources]]
id = "web"
kind = "web"
title = "Local website"
seeds = ["http://127.0.0.1:{port}{seed_path}"]
allowed_hosts = ["127.0.0.1", "localhost"]
{redirect_config}
{source_capture}
""".strip()
        path = self.root / "collections.toml"
        path.write_text(config, encoding="utf-8")
        return path

    def execute_plan(self, seed_path: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        config = load_config(self.write_config(seed_path))
        plan = plan_capture(
            config,
            "local-fixture",
            capture_id="20260827T120000Z-a1b2c3",
        )
        plan.capture_directory.mkdir(parents=True)
        command = plan.commands[0]
        for directory in command.required_directories:
            directory.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            command.argv,
            cwd=command.working_directory,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result, command.working_directory

    def capture_diagnostics(self, capture: object) -> str:
        capture_directory = getattr(capture, "capture_directory")
        metadata = getattr(capture, "metadata")
        log_path = capture_directory / "sources/web/logs/wget.log"
        try:
            log = log_path.read_text(encoding="utf-8", errors="replace")[-8_192:]
        except OSError as exc:
            log = f"<cannot read {log_path}: {exc}>"
        return f"source={metadata['sources'][0]!r}\nwget.log:\n{log}"

    def test_recursive_plan_creates_mirror_warc_cdx_and_log(self) -> None:
        config = load_config(self.write_config("/index.html"))
        capture = execute_capture(
            config,
            "local-fixture",
            capture_id="20260827T120000Z-a1b2c3",
            operator_note="Local integration capture",
        )
        source_root = capture.capture_directory / "sources/web"

        self.assertEqual(capture.status, "complete", self.capture_diagnostics(capture))
        self.assertIn("/index.html", self.requests)
        self.assertIn("/child.html", self.requests)
        self.assertIn("/style.css", self.requests)
        self.assertTrue(any((source_root / "mirror").rglob("index.html")))
        self.assertTrue(any((source_root / "mirror").rglob("child.html")))
        self.assertTrue(list((source_root / "warc").glob("capture*.warc.gz")))
        self.assertTrue(list((source_root / "warc").glob("capture*.cdx")))
        self.assertTrue((source_root / "logs/wget.log").is_file())
        self.assertTrue((capture.capture_directory / "capture.json").is_file())
        self.assertTrue((source_root / "metadata/command.json").is_file())
        self.assertTrue((source_root / "metadata/result.json").is_file())
        self.assertTrue((source_root / "seeds.txt").is_file())
        self.assertTrue((capture.capture_directory / "manifest-sha256.json").is_file())
        self.assertTrue((capture.capture_directory / "SHA256SUMS").is_file())
        verification = verify_capture(capture.capture_directory)
        self.assertTrue(verification.ok, verification.errors)
        latest = capture.capture_directory.parent.parent / "LATEST-ACQUIRED"
        self.assertEqual(latest.read_text(encoding="utf-8").strip(), "captures/20260827T120000Z-a1b2c3")
        source_latest = capture.capture_directory.parent.parent / "LATEST-web"
        self.assertEqual(
            source_latest.read_text(encoding="utf-8").strip(),
            "captures/20260827T120000Z-a1b2c3",
        )
        self.assertEqual(capture.source_latest_updated, ("web",))
        self.assertTrue(verify_capture(latest.parent).ok)

        source_result = capture.metadata["sources"][0]
        mirror_files = [path for path in (source_root / "mirror").rglob("*") if path.is_file()]
        warc_files = list((source_root / "warc").glob("capture*.warc.gz"))
        cdx_files = list((source_root / "warc").glob("capture*.cdx"))
        self.assertEqual(source_result["downloaded_files"], len(mirror_files))
        self.assertEqual(
            source_result["downloaded_bytes"],
            sum(path.stat().st_size for path in mirror_files),
        )
        self.assertEqual(source_result["payloads"]["mirror"]["files"], len(mirror_files))
        self.assertEqual(source_result["payloads"]["warc"]["files"], len(warc_files))
        self.assertEqual(
            source_result["payloads"]["warc"]["bytes"],
            sum(path.stat().st_size for path in warc_files),
        )
        self.assertEqual(source_result["payloads"]["warc"]["cdx_files"], len(cdx_files))
        self.assertGreater(source_result["payloads"]["warc"]["indexed_records"], 0)
        self.assertTrue(source_result["payloads"]["warc"]["has_response_or_resource"])

    def test_warc_only_capture_has_substantive_metrics(self) -> None:
        config = load_config(self.write_config("/index.html", mirror=False))

        capture = execute_capture(
            config,
            "local-fixture",
            capture_id="20260827T120000Z-w1r2c3",
        )

        source = capture.metadata["sources"][0]
        warc = source["payloads"]["warc"]
        self.assertEqual(capture.status, "complete", self.capture_diagnostics(capture))
        self.assertEqual(source["payloads"]["mirror"], {"files": 0, "bytes": 0})
        self.assertEqual(source["downloaded_files"], 0)
        self.assertEqual(source["downloaded_bytes"], 0)
        self.assertGreater(warc["files"], 0)
        self.assertGreater(warc["bytes"], 0)
        self.assertGreater(warc["cdx_files"], 0)
        self.assertGreater(warc["indexed_records"], 0)
        self.assertTrue(warc["has_response_or_resource"])

    def test_source_filtered_capture_does_not_update_latest(self) -> None:
        config = load_config(self.write_config("/index.html"))

        capture = execute_capture(
            config,
            "local-fixture",
            source_ids=["web"],
            capture_id="20260827T120000Z-b2c3d4",
        )

        self.assertEqual(capture.status, "complete", self.capture_diagnostics(capture))
        self.assertFalse(capture.latest_updated)
        self.assertFalse((capture.capture_directory.parent.parent / "LATEST").exists())
        self.assertFalse(
            (capture.capture_directory.parent.parent / "LATEST-ACQUIRED").exists()
        )
        source_latest = capture.capture_directory.parent.parent / "LATEST-web"
        self.assertEqual(
            source_latest.read_text(encoding="utf-8").strip(),
            "captures/20260827T120000Z-b2c3d4",
        )

    def test_plan_does_not_follow_redirects(self) -> None:
        result, source_root = self.execute_plan("/redirect")

        self.assertNotEqual(result.returncode, 0)
        log_path = source_root / "logs/wget.log"
        log = log_path.read_text(encoding="utf-8", errors="replace")[-8_192:]
        self.assertIn(
            "/redirect",
            self.requests,
            f"returncode={result.returncode}; stderr={result.stderr!r}; wget.log:\n{log}",
        )
        self.assertNotIn("/redirect-target", self.requests)

    def test_reviewed_edge_is_requested_after_proposal_and_unreviewed_second_hop_stops(self) -> None:
        port = self.server.server_port
        first = f"http://127.0.0.1:{port}/redirect"
        reviewed_target = f"http://localhost:{port}/redirect-target"
        config = load_config(
            self.write_config(
                "/redirect",
                reviewed_redirects=((first, reviewed_target),),
            )
        )

        capture = execute_capture(
            config,
            "local-fixture",
            capture_id="20260827T120000Z-r1d2r3",
        )

        self.assertEqual(capture.status, "partial")
        self.assertLess(self.requests.index("/redirect"), self.requests.index("/redirect-target"))
        self.assertNotIn("/second-target", self.requests)
        redirects = json.loads(
            (
                capture.capture_directory / "sources/web/metadata/redirects.json"
            ).read_text(encoding="utf-8")
        )
        proposals = redirects["proposals"]
        self.assertEqual(len(proposals), 2)
        self.assertEqual(
            (proposals[0]["from"], proposals[0]["to"]),
            (first, reviewed_target),
        )
        self.assertTrue(proposals[0]["reviewed"])
        self.assertTrue(proposals[0]["requested"])
        self.assertFalse(proposals[1]["reviewed"])
        self.assertFalse(proposals[1]["requested"])
        private_command = json.loads(
            (
                capture.capture_directory / "sources/web/metadata/private/command.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(len(private_command["redirect_commands"]), 1)
        self.assertIn("--max-redirect=0", private_command["redirect_commands"][0]["argv"])
        redirect_quota = next(
            value
            for value in private_command["redirect_commands"][0]["argv"]
            if value.startswith("--quota=")
        )
        self.assertNotEqual(redirect_quota, "--quota=5M")
        self.assertLess(int(redirect_quota.removeprefix("--quota=")), 5 * 1024 * 1024)
        self.assertEqual(
            private_command["redirect_commands"][0]["request_urls"],
            [reviewed_target],
        )

    def test_reviewed_redirect_to_successful_target_is_complete_with_warning(self) -> None:
        port = self.server.server_port
        first = f"http://127.0.0.1:{port}/single-redirect"
        reviewed_target = f"http://localhost:{port}/single-target"
        config = load_config(
            self.write_config(
                "/single-redirect",
                reviewed_redirects=((first, reviewed_target),),
            )
        )

        capture = execute_capture(
            config,
            "local-fixture",
            capture_id="20260827T120000Z-s1d2s3",
        )

        self.assertEqual(capture.status, "complete_with_warnings")
        self.assertLess(
            self.requests.index("/single-redirect"),
            self.requests.index("/single-target"),
        )


if __name__ == "__main__":
    unittest.main()
