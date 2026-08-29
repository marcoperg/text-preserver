from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from text_preserver.adapter_process import (
    AdapterLimits,
    AdapterProcessError,
    adapter_digest,
    invoke_adapter,
)
from text_preserver.access.reader_model import reader_model_identity
from text_preserver.access.reader_shell import reader_shell_identity
from text_preserver.preservation.recipe_bundle import scan_recipe_directory


class AdapterProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.recipe = self.root / "recipe"
        self.capture = self.root / "capture"
        self.recipe.mkdir()
        self.capture.mkdir()

    def write_adapter(self, source: str, name: str = "adapter.py") -> Path:
        path = self.recipe / name
        path.write_text(source, encoding="utf-8")
        return path

    def invoke(
        self,
        path: Path,
        *,
        operation: str = "validate",
        recipe_api: int = 1,
        context: dict[str, object] | None = None,
        arguments: dict[str, object] | None = None,
        limits: AdapterLimits | None = None,
        allow_python_network: bool = False,
        allow_python_subprocess: bool = False,
    ):
        return invoke_adapter(
            path,
            adapter_sha256=adapter_digest(path),
            recipe_api=recipe_api,
            operation=operation,
            context=context or {"capture_directory": str(self.capture)},
            arguments=arguments,
            limits=limits,
            allow_python_network=allow_python_network,
            allow_python_subprocess=allow_python_subprocess,
        )

    def test_api1_validate_is_separate_and_preserves_recipe_imports_and_assets(self) -> None:
        (self.recipe / "asset.txt").write_text("asset value", encoding="utf-8")
        (self.recipe / "helper.py").write_text("VALUE = 'relative import'\n", encoding="utf-8")
        adapter = self.write_adapter(
            """
from pathlib import Path
import os
import sys
from .helper import VALUE

def analyze_capture(capture_directory, *, expected):
    print("adapter stdout")
    print("adapter stderr", file=sys.stderr)
    return {
        "capture": capture_directory.name,
        "expected": expected,
        "helper": VALUE,
        "asset": Path("asset.txt").read_text(encoding="utf-8"),
        "pid": os.getpid(),
    }
"""
        )

        result = self.invoke(adapter, arguments={"expected": 7})

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.result["capture"], "capture")
        self.assertEqual(result.result["helper"], "relative import")
        self.assertEqual(result.result["asset"], "asset value")
        self.assertNotEqual(result.result["pid"], os.getpid())
        self.assertEqual(result.diagnostics["stdout"], "adapter stdout\n")
        self.assertEqual(result.diagnostics["stderr"], "adapter stderr\n")
        self.assertTrue(result.controls["process_separation"]["enforced"])
        self.assertFalse(result.controls["full_os_sandbox"]["enforced"])

    def test_api1_render_and_stream(self) -> None:
        adapter = self.write_adapter(
            """
def render_static_reader(capture_directory, *, title):
    return {"files": {"index.html": title}, "capture": capture_directory.name}

def write_static_reader(capture_directory, *, output_directory, title):
    (output_directory / "index.html").write_text(title, encoding="utf-8")
    return {"capture": capture_directory.name}
"""
        )
        rendered = self.invoke(adapter, operation="render", arguments={"title": "Reader"})
        self.assertTrue(rendered.ok, rendered.error)
        self.assertEqual(rendered.result["files"]["index.html"], "Reader")

        output = self.root / "output"
        output.mkdir()
        streamed = self.invoke(
            adapter,
            operation="stream",
            context={
                "capture_directory": str(self.capture),
                "output_directory": str(output),
            },
            arguments={"title": "Streamed"},
        )
        self.assertTrue(streamed.ok, streamed.error)
        self.assertEqual((output / "index.html").read_text(encoding="utf-8"), "Streamed")

    def test_generic_api2_operation(self) -> None:
        adapter = self.write_adapter(
            """
def handle_operation(operation, *, context, arguments):
    return {"operation": operation, "context": context, "arguments": arguments}
"""
        )
        result = self.invoke(
            adapter,
            recipe_api=2,
            operation="inventory",
            context={"collection_id": "example"},
            arguments={"minimum": 3},
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.result["operation"], "inventory")
        self.assertEqual(result.result["context"]["collection_id"], "example")

    def test_reader_shell_source_is_bound_to_expected_digest(self) -> None:
        adapter = self.write_adapter(
            """
from text_preserver.access.reader_shell import reader_stylesheet
from text_preserver.adapters import ReaderReport

def build_reader(context):
    return ReaderReport(
        "complete",
        {"stylesheet_bytes": len(reader_stylesheet())},
        (),
        {"index.html": "reader"},
    )
"""
        )
        output = self.root / "reader-output"
        output.mkdir()
        context = {
            "capture_directory": str(self.capture),
            "output_directory": str(output),
            "expected_work_count": 1,
        }

        mismatch = invoke_adapter(
            adapter,
            adapter_sha256=adapter_digest(adapter),
            recipe_api=2,
            operation="reader",
            context=context,
            reader_support={
                "text_preserver.access.reader_model": str(reader_model_identity()["sha256"]),
                "text_preserver.access.reader_shell": "f" * 64,
            },
        )
        with self.assertRaisesRegex(AdapterProcessError, "reader_support"):
            invoke_adapter(
                adapter,
                adapter_sha256=adapter_digest(adapter),
                recipe_api=2,
                operation="reader",
                context=context,
            )
        matched = invoke_adapter(
            adapter,
            adapter_sha256=adapter_digest(adapter),
            recipe_api=2,
            operation="reader",
            context=context,
            reader_support={
                "text_preserver.access.reader_model": str(reader_model_identity()["sha256"]),
                "text_preserver.access.reader_shell": str(reader_shell_identity()["sha256"]),
            },
        )

        self.assertFalse(mismatch.ok)
        assert mismatch.error is not None
        self.assertIn("reader support module", mismatch.error["message"])
        self.assertTrue(matched.ok, matched.error)

    def test_typed_api2_validation_operation(self) -> None:
        adapter = self.write_adapter(
            """
from text_preserver.adapters import ValidationContext, ValidationReport

def validate(context: ValidationContext) -> ValidationReport:
    return ValidationReport(
        "complete",
        (),
        ("typed",),
        {"capture": context.capture_directory.name,
         "required": list(context.required_source_ids)},
    )
"""
        )
        result = self.invoke(
            adapter,
            recipe_api=2,
            operation="validate",
            context={
                "capture_directory": str(self.capture),
                "expected_work_count": 2,
                "required_representation_kinds": ["text"],
                "required_source_ids": ["source"],
            },
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.result["status"], "complete")
        self.assertEqual(result.result["warnings"], ["typed"])
        self.assertEqual(result.result["capture"], "capture")

    def test_validation_denies_filesystem_mutation_and_preserves_bytes(self) -> None:
        target = self.capture / "preserved.txt"
        target.write_text("original", encoding="utf-8")
        adapter = self.write_adapter(
            """
def analyze_capture(capture_directory):
    denied = False
    try:
        (capture_directory / "preserved.txt").write_text("changed", encoding="utf-8")
    except PermissionError:
        denied = True
    return {"denied": denied}
"""
        )

        result = self.invoke(adapter)

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.result["denied"])
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        filesystem = result.controls["worker"]["filesystem"]
        self.assertEqual(filesystem["scope"], "language-runtime policy")
        self.assertFalse(filesystem["os_enforced"])

    @unittest.skipUnless(os.name == "posix", "requires the POSIX runtime module")
    def test_posix_module_cannot_bypass_mutation_policy(self) -> None:
        target = self.capture / "preserved.txt"
        target.write_text("original", encoding="utf-8")
        adapter = self.write_adapter(
            """
def analyze_capture(capture_directory):
    import os
    import posix
    denied = []
    try:
        descriptor = posix.open(
            str(capture_directory / "preserved.txt"),
            os.O_WRONLY | os.O_TRUNC,
        )
        posix.write(descriptor, b"changed")
        posix.close(descriptor)
    except PermissionError:
        denied.append("write")
    try:
        posix.fork()
    except PermissionError:
        denied.append("fork")
    descriptor = posix.open(str(capture_directory / "preserved.txt"), os.O_RDONLY)
    try:
        posix.fchmod(descriptor, 0)
    except PermissionError:
        denied.append("fchmod")
    finally:
        posix.close(descriptor)
    if hasattr(posix, "chflags"):
        try:
            posix.chflags(str(capture_directory / "preserved.txt"), 0)
        except PermissionError:
            denied.append("chflags")
    return {"denied": denied}
"""
        )

        result = self.invoke(adapter)

        self.assertTrue(result.ok, result.error)
        expected = ["write", "fork", "fchmod"]
        if hasattr(os, "chflags"):
            expected.append("chflags")
        self.assertEqual(result.result["denied"], expected)
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertNotEqual(target.stat().st_mode & 0o777, 0)

    def test_reader_can_write_only_in_dedicated_output(self) -> None:
        external = self.root / "external.txt"
        external.write_text("original", encoding="utf-8")
        adapter = self.write_adapter(
            f"""
from pathlib import Path

def write_static_reader(capture_directory, *, output_directory):
    (output_directory / "index.html").write_text("reader", encoding="utf-8")
    denied = False
    try:
        Path({str(external)!r}).write_text("changed", encoding="utf-8")
    except PermissionError:
        denied = True
    return {{"denied": denied}}
"""
        )
        output = self.root / "reader-output"
        output.mkdir()

        result = self.invoke(
            adapter,
            operation="stream",
            context={
                "capture_directory": str(self.capture),
                "output_directory": str(output),
            },
        )

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.result["denied"])
        self.assertEqual((output / "index.html").read_text(encoding="utf-8"), "reader")
        self.assertEqual(external.read_text(encoding="utf-8"), "original")

    def test_digest_is_checked_before_and_inside_worker(self) -> None:
        adapter = self.write_adapter("def analyze_capture(capture_directory): return {}\n")
        with self.assertRaisesRegex(AdapterProcessError, "digest does not match"):
            invoke_adapter(
                adapter,
                adapter_sha256="0" * 64,
                recipe_api=1,
                operation="validate",
                context={"capture_directory": str(self.capture)},
            )

    def test_changed_recipe_sibling_is_rejected_before_execution(self) -> None:
        (self.recipe / "helper.py").write_text("VALUE = 'first'\n", encoding="utf-8")
        adapter = self.write_adapter(
            "from .helper import VALUE\n"
            "def analyze_capture(capture_directory): return {'value': VALUE}\n"
        )
        bundle_sha256 = scan_recipe_directory(self.recipe).sha256
        (self.recipe / "helper.py").write_text("VALUE = 'changed'\n", encoding="utf-8")

        with self.assertRaisesRegex(AdapterProcessError, "bundle changed"):
            invoke_adapter(
                adapter,
                adapter_sha256=adapter_digest(adapter),
                recipe_api=1,
                operation="validate",
                context={"capture_directory": str(self.capture)},
                bundle_root=self.recipe,
                bundle_sha256=bundle_sha256,
            )

    def test_python_network_and_subprocess_creation_are_denied_by_default(self) -> None:
        adapter = self.write_adapter(
            """
def analyze_capture(capture_directory):
    import socket
    import subprocess
    denied = []
    for name, call in (
        ("socket", lambda: socket.socket()),
        ("subprocess", lambda: subprocess.run(["true"])),
    ):
        try:
            call()
        except PermissionError:
            denied.append(name)
    return {"denied": denied}
"""
        )
        result = self.invoke(adapter)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.result["denied"], ["socket", "subprocess"])
        worker = result.controls["worker"]
        self.assertEqual(worker["network"]["scope"], "language-runtime policy")
        self.assertFalse(worker["network"]["os_enforced"])
        self.assertFalse(worker["full_os_sandbox"])

    def test_timeout_returns_a_normal_error_and_terminates_process_group(self) -> None:
        adapter = self.write_adapter(
            """
def analyze_capture(capture_directory):
    while True:
        pass
"""
        )
        started = time.monotonic()
        result = self.invoke(
            adapter,
            limits=AdapterLimits(wall_seconds=0.2),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "timeout")
        self.assertLess(time.monotonic() - started, 3)
        if os.name == "posix":
            self.assertTrue(result.controls["process_group_termination"]["used"])

    def test_diagnostics_and_protocol_results_are_bounded(self) -> None:
        noisy = self.write_adapter(
            """
def analyze_capture(capture_directory):
    print("x" * 10000)
    return {"ok": True}
"""
        )
        diagnostic_result = self.invoke(
            noisy,
            limits=AdapterLimits(max_diagnostic_bytes=128),
        )
        self.assertTrue(diagnostic_result.ok, diagnostic_result.error)
        self.assertEqual(len(diagnostic_result.diagnostics["stdout"].encode()), 128)
        self.assertTrue(diagnostic_result.diagnostics["stdout_truncated"])

        large = self.write_adapter(
            """
def analyze_capture(capture_directory):
    return {"value": "x" * 100000}
""",
            name="large.py",
        )
        protocol_result = self.invoke(
            large,
            limits=AdapterLimits(max_protocol_bytes=4096),
        )
        self.assertFalse(protocol_result.ok)
        self.assertEqual(protocol_result.error["code"], "protocol_output_limit")
        self.assertLess(len(json.dumps(protocol_result.envelope()).encode()), 4096)

    def test_streamed_output_object_count_is_bounded_during_execution(self) -> None:
        adapter = self.write_adapter(
            """
def write_static_reader(capture_directory, *, output_directory):
    for index in range(2100):
        (output_directory / f"{index}.txt").write_text("", encoding="utf-8")
    return {"status": "complete", "summary": {}, "warnings": []}
"""
        )
        output = self.root / "bounded-output"
        output.mkdir()

        result = self.invoke(
            adapter,
            operation="stream",
            context={
                "capture_directory": str(self.capture),
                "output_directory": str(output),
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "adapter_output_limit")

    def test_non_json_result_is_a_structured_failure(self) -> None:
        adapter = self.write_adapter(
            """
def analyze_capture(capture_directory):
    return {"bad": object()}
"""
        )
        result = self.invoke(adapter)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "invalid_result")

    def test_api2_handle_operation_cannot_replace_typed_validation(self) -> None:
        adapter = self.write_adapter(
            """
def handle_operation(operation, *, context, arguments):
    return {"status": "complete", "errors": [], "warnings": []}
"""
        )
        result = self.invoke(
            adapter,
            recipe_api=2,
            operation="validate",
            context={
                "capture_directory": str(self.capture),
                "expected_work_count": 0,
                "required_representation_kinds": [],
                "required_source_ids": [],
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "missing_capability")

    def test_worker_rejects_unknown_envelope_fields(self) -> None:
        request = {
            "protocol": "text-preserver.adapter",
            "version": 2,
            "kind": "request",
            "request_id": "1" * 32,
            "unexpected": True,
        }
        completed = subprocess.run(
            [sys.executable, "-m", "text_preserver.adapter_worker"],
            input=json.dumps(request).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        response = json.loads(completed.stdout)
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(response["request_id"], "0" * 32)


if __name__ == "__main__":
    unittest.main()
