"""Exercise installed public assets and a local capture-to-reader flow."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading

import text_preserver
from text_preserver.analysis import analyze_preservation, build_static_reader
from text_preserver.capture import execute_capture
from text_preserver.config import load_config
from text_preserver.manifest import verify_capture


PUBLIC_RECIPE_FILES = {
    "etcsl": {
        "README.md",
        "collection.toml",
        "fixtures/catalogue.html",
        "fixtures/expected-ids.txt",
        "inventory.py",
        "reader.py",
        "rules/preservation.pl",
        "seeds/repository.txt",
        "seeds/web.txt",
    },
    "gretil": {
        "README.md",
        "collection.toml",
        "fixtures/catalogue.html",
        "fixtures/reviewed-tei-ids.txt",
        "fixtures/sample-expected-ids.txt",
        "inventory.py",
        "reader.py",
        "seeds/bulk-packages.txt",
        "seeds/current-register.txt",
        "seeds/dictionaries.txt",
        "seeds/frozen-register.txt",
    },
    "sacred-texts": {
        "README.md",
        "collection.toml",
        "inventory.py",
        "seeds/internet-archive-2021.txt",
        "seeds/wayback-download-recovery.txt",
    },
}


ADAPTER = r'''
from pathlib import Path


def _payload(capture_directory: Path) -> str:
    matches = list((capture_directory / "sources/web/mirror").rglob("payload.txt"))
    if len(matches) != 1:
        raise ValueError(f"expected one payload, found {len(matches)}")
    return matches[0].read_text(encoding="utf-8")


def analyze_capture(capture_directory, **_kwargs):
    template = (Path(__file__).parent / "template.txt").read_text(encoding="utf-8")
    errors = [] if _payload(capture_directory) == "preserved payload\n" else ["payload mismatch"]
    return {
        "status": "complete" if not errors else "incomplete",
        "errors": errors,
        "warnings": [],
        "template": template,
    }


def render_static_reader(capture_directory, **_kwargs):
    template = (Path(__file__).parent / "template.txt").read_text(encoding="utf-8")
    return {
        "status": "complete",
        "files": {"index.html": f"<h1>{template}</h1><p>{_payload(capture_directory)}</p>"},
        "summary": {"item_count": 1},
        "warnings": [],
    }
'''.strip()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        elif self.path == "/payload.txt":
            self._send(b"preserved payload\n", "text/plain")
        else:
            self.send_error(404)

    def _send(self, content: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def verify_public_recipes(root: Path) -> None:
    config_path = root / "public.toml"
    config_path.write_text(
        '''
recipes = ["public:etcsl", "public:gretil", "public:sacred-texts"]

[project]
archive_root = "./public-archive"
derived_root = "./public-derived"
workspace_root = "./public-workspace"
operator = "Installed smoke test"
contact = "mailto:test@example.org"
user_agent = "text-preserver-installed-smoke/1.0"
'''.strip(),
        encoding="utf-8",
    )
    collections = load_config(config_path).collections
    assert {collection.id for collection in collections} == set(PUBLIC_RECIPE_FILES)
    for collection in collections:
        assert collection.recipe_api == 1
        assert collection.recipe_path is not None
        recipe_root = collection.recipe_path.parent
        installed = {
            path.relative_to(recipe_root).as_posix()
            for path in recipe_root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        assert installed == PUBLIC_RECIPE_FILES[collection.id], (
            collection.id,
            sorted(installed),
        )


def run_fixture_flow(root: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        recipe = root / "recipe"
        recipe.mkdir()
        (recipe / "adapter.py").write_text(ADAPTER + "\n", encoding="utf-8")
        (recipe / "template.txt").write_text("Installed fixture", encoding="utf-8")
        (recipe / "collection.toml").write_text(
            f'''
recipe_api = 1

[collection]
id = "installed-fixture"
title = "Installed Fixture"

[collection.analysis]
inventory_adapter = "adapter.py"

[[collection.sources]]
id = "web"
kind = "http-file"
title = "Local payload"
seeds = ["http://127.0.0.1:{server.server_port}/payload.txt"]
allowed_hosts = ["127.0.0.1"]

[collection.sources.capture]
recursive = false
page_requisites = false
convert_links = false
adjust_extension = false
wait = 0
random_wait = false
tries = 1
timeout = 5
quota = "1M"
warc_max_size = "1M"
'''.strip(),
            encoding="utf-8",
        )
        config_path = root / "fixture.toml"
        config_path.write_text(
            '''
recipes = ["recipe/collection.toml"]

[project]
archive_root = "./archive"
derived_root = "./derived"
workspace_root = "./workspace"
operator = "Installed smoke test"
contact = "mailto:test@example.org"
user_agent = "text-preserver-installed-smoke/1.0"
'''.strip(),
            encoding="utf-8",
        )
        config = load_config(config_path)
        capture = execute_capture(
            config,
            "installed-fixture",
            capture_id="20260829T120000Z-a1b2c3",
        )
        assert capture.status == "complete"
        assert verify_capture(capture.capture_directory).ok
        bundle = capture.capture_directory / "metadata/recipe-bundle"
        assert (bundle / "adapter.py").is_file()
        assert (bundle / "template.txt").is_file()
        manifest = json.loads(
            (capture.capture_directory / "metadata/recipe-bundle-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["recipe_api"] == 1

        validation = analyze_preservation(config, "installed-fixture")
        assert validation.status == "complete"
        assert validation.report["template"] == "Installed fixture"
        assert validation.report_path.is_file()
        reader = build_static_reader(config, "installed-fixture")
        assert reader.metadata["status"] == "complete"
        assert "Installed fixture" in reader.index_path.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    print(f"Testing installed package: {Path(text_preserver.__file__).resolve()}")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        verify_public_recipes(root)
        run_fixture_flow(root)
    print("Installed wheel smoke test passed")


if __name__ == "__main__":
    main()
