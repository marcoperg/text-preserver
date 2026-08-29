from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
import zipfile

from text_preserver.preservation.capture import plan_capture
from text_preserver.adapters import _load_adapter
from text_preserver.config import load_config
from text_preserver.recipes import public_recipe_path


REPOSITORY_ROOT = Path(__file__).parents[1]
RECIPE_ROOT = public_recipe_path("gretil").parent
inventory, _INVENTORY_SOURCE = _load_adapter(RECIPE_ROOT / "validator.py")
reader, _READER_SOURCE = _load_adapter(RECIPE_ROOT / "reader.py")


class GretilInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = (RECIPE_ROOT / "fixtures/catalogue.html").read_text(
            encoding="utf-8"
        )
        self.expected_ids = (RECIPE_ROOT / "fixtures/sample-expected-ids.txt").read_text(
            encoding="utf-8"
        ).splitlines()

    def test_extracts_representation_lineages_and_normalizes_alias(self) -> None:
        result = inventory.extract_inventory(self.document)

        self.assertEqual(list(result.tei_ids), self.expected_ids)
        self.assertEqual(result.analytic_html_ids, result.tei_ids)
        self.assertEqual(result.plaintext_ids, result.tei_ids)
        self.assertEqual(len(result.bulk_packages), 8)
        self.assertEqual(len(result.dictionaries), 21)
        self.assertTrue(
            all("gretil.sub.uni-goettingen.de" in url for url in result.first_party_urls)
        )
        self.assertFalse(any("example.org" in url for url in result.first_party_urls))

    def test_fixture_report_is_complete(self) -> None:
        result = inventory.build_report(
            inventory.extract_inventory(self.document),
            expected_work_count=3,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_report_rejects_substituted_identifier_and_dictionary(self) -> None:
        extracted = inventory.extract_inventory(self.document)
        expected_hash = hashlib.sha256(
            ("\n".join(extracted.tei_ids) + "\n").encode()
        ).hexdigest()
        substituted = inventory.RegisterInventory(
            extracted.first_party_urls,
            (*extracted.tei_ids[:-1], "xct_Substituted"),
            extracted.analytic_html_ids,
            extracted.plaintext_ids,
            extracted.bulk_packages,
            (*extracted.dictionaries[:-1], "unexpected.dict.xdxf"),
        )

        result = inventory.build_report(
            substituted,
            expected_work_count=3,
            expected_tei_id_sha256=expected_hash,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(any("identifier set" in value for value in result["errors"]))
        self.assertTrue(any("dictionary" in value for value in result["errors"]))

    def test_required_plaintext_detects_lineage_gap(self) -> None:
        extracted = inventory.extract_inventory(self.document)
        incomplete = inventory.RegisterInventory(
            extracted.first_party_urls,
            extracted.tei_ids,
            extracted.analytic_html_ids,
            extracted.plaintext_ids[:-1],
            extracted.bulk_packages,
            extracted.dictionaries,
        )

        result = inventory.build_report(
            incomplete,
            expected_work_count=3,
            required_representation_kinds=("tei", "plaintext"),
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertIn("no plain-text transformation", result["errors"][0])

    def test_zip_analysis_rejects_unsafe_member_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../escape.txt", "bad")

            with self.assertRaisesRegex(inventory.InventoryError, "unsafe archive path"):
                inventory._analyze_zip(path)

    def test_zip_analysis_validates_reviewed_tei_mapping(self) -> None:
        filename_id = "sa_bhAgavatapurANa-10,29-33"
        register_id, root_id = inventory.BULK_TEI_MAPPINGS[filename_id]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mapped.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    f"1_sanskr/tei/{filename_id}.xml",
                    (
                        '<TEI xmlns="http://www.tei-c.org/ns/1.0" '
                        f'xml:id="{root_id}" />'
                    ),
                )

            report = inventory._analyze_zip(path)

            self.assertEqual(report["tei_ids"], [register_id])
            self.assertEqual(report["tei_root_ids"][filename_id], root_id)

    def test_zip_analysis_rejects_tei_without_internal_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-id.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "1_sanskr/tei/sa_example.xml",
                    '<TEI xmlns="http://www.tei-c.org/ns/1.0" />',
                )

            with self.assertRaisesRegex(inventory.InventoryError, "no xml:id"):
                inventory._analyze_zip(path)

    def test_zip_analysis_rejects_unmapped_wrong_root_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-id.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "1_sanskr/tei/sa_expected.xml",
                    (
                        '<TEI xmlns="http://www.tei-c.org/ns/1.0" '
                        'xml:id="sa_unrelated" />'
                    ),
                )

            with self.assertRaisesRegex(inventory.InventoryError, "reviewed mapping"):
                inventory._analyze_zip(path)

    def test_analyzes_complete_fixture_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "20260828T120000Z-a1b2c3"
            current = capture / "sources/current-register/mirror/example"
            bulk = capture / "sources/bulk-packages/mirror/example/gretil"
            dictionaries = capture / "sources/dictionaries/mirror/example/gretil"
            frozen = capture / "sources/frozen-register/mirror/example"
            for path in (current, bulk, dictionaries, frozen):
                path.mkdir(parents=True)
            shutil.copyfile(RECIPE_ROOT / "fixtures/catalogue.html", current / "gretil.html")
            for name in inventory.EXPECTED_CURRENT_FILES:
                if name != "gretil.html":
                    (current / name).write_bytes(b"fixture")
            for name in inventory.EXPECTED_BULK_PACKAGES:
                with zipfile.ZipFile(bulk / name, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(f"{name}.txt", "fixture")
                    if name == inventory.EXPECTED_BULK_PACKAGES[0]:
                        for identifier in self.expected_ids:
                            root_id = inventory.BULK_TEI_ROOT_ID_EXCEPTIONS.get(
                                identifier,
                                identifier,
                            )
                            archive.writestr(
                                f"1_sanskr/tei/{identifier}.xml",
                                (
                                    '<TEI xmlns="http://www.tei-c.org/ns/1.0" '
                                    f'xml:id="{root_id}"><teiHeader><fileDesc>'
                                    f"<titleStmt><title>{identifier}</title></titleStmt>"
                                    '<publicationStmt><availability status="free">'
                                    "<p>Fixture rights</p></availability></publicationStmt>"
                                    "<sourceDesc><p>Fixture source</p></sourceDesc>"
                                    "</fileDesc></teiHeader><text><body>"
                                    "<p>Fixture body</p></body></text></TEI>"
                                ),
                            )
            for name in inventory.EXPECTED_DICTIONARIES:
                (dictionaries / name).write_text("<xdxf />", encoding="utf-8")
            for name in inventory.EXPECTED_FROZEN_FILES:
                (frozen / name).write_bytes(b"fixture")
            source_ids = (
                "current-register",
                "bulk-packages",
                "dictionaries",
                "frozen-register",
            )
            (capture / "capture.json").write_text(
                json.dumps(
                    {
                        "capture_id": capture.name,
                        "sources": [
                            {"source_id": source_id, "status": "complete"}
                            for source_id in source_ids
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = inventory.analyze_capture(
                capture,
                expected_work_count=3,
                required_representation_kinds=("tei",),
                required_source_ids=source_ids,
            )

            self.assertEqual(report["status"], "complete")
            self.assertEqual(len(report["bulk_packages"]), 8)
            self.assertEqual(report["dictionaries"]["count"], 21)
            self.assertEqual(report["bulk_tei"]["count"], 3)

            reader_output = Path(directory) / "reader"
            reader_payload = reader.write_static_reader(
                capture,
                output_directory=reader_output,
                expected_work_count=3,
            )
            self.assertEqual(reader_payload["status"], "complete_with_warnings")
            self.assertEqual(reader_payload["summary"]["work_count"], 3)
            self.assertEqual(len(list((reader_output / "works").glob("*.html"))), 3)
            self.assertIn(self.expected_ids[0], (reader_output / "index.html").read_text())
            self.assertTrue((reader_output / "about.html").is_file())
            self.assertNotIn("<script", (reader_output / "index.html").read_text())

            with zipfile.ZipFile(
                bulk / inventory.EXPECTED_BULK_PACKAGES[0],
                "w",
            ) as archive:
                archive.writestr("unrelated.txt", "fixture")
            missing_tei = inventory.analyze_capture(
                capture,
                expected_work_count=3,
                required_representation_kinds=("tei",),
                required_source_ids=source_ids,
            )
            self.assertEqual(missing_tei["status"], "incomplete")
            self.assertTrue(
                any("absent from bulk" in value for value in missing_tei["errors"])
            )

    def test_reader_renderer_preserves_mixed_content_and_escapes_markup(self) -> None:
        element = ElementTree.fromstring(
            """
<p xmlns="http://www.tei-c.org/ns/1.0">before &lt;unsafe&gt;
  <supplied>inserted</supplied> tail
  <app><lem wit="#A">lemma</lem><rdg wit="#B">reading</rdg></app>
  <ref target="#local">reference</ref><date when-iso="2020-09-10"/>
  <hi rend="smaller">small text</hi>
  <gap reason="lost"/><pb n="12"/>
</p>
""".strip()
        )

        rendered = reader._render_tei_element(element)

        self.assertIn("before &lt;unsafe&gt;", rendered)
        self.assertIn("inserted", rendered)
        self.assertIn("tail", rendered)
        self.assertIn("lemma", rendered)
        self.assertIn("#A", rendered)
        self.assertIn("target: #local", rendered)
        self.assertIn("2020-09-10", rendered)
        self.assertIn('data-tei-rend="smaller"', rendered)
        self.assertIn("gap: lost", rendered)
        self.assertIn("page 12", rendered)
        self.assertNotIn("<unsafe>", rendered)

        availability = ElementTree.fromstring(
            """
<availability xmlns="http://www.tei-c.org/ns/1.0" status="restricted">
  <licence target="https://example.org/licence">Local terms</licence>
</availability>
""".strip()
        )
        rights = reader._reader_details("Availability", availability)
        self.assertIn("status: restricted", rights)
        self.assertIn("licence target: https://example.org/licence", rights)

        line_group = ElementTree.fromstring(
            """
<lg xmlns="http://www.tei-c.org/ns/1.0" rend="bold" xml:id="verse-1">
  <l><corr resp="#VGA">corrected</corr><pb n="3" edRef="#Apte1929"/></l>
</lg>
""".strip()
        )
        block = reader._render_tei_element(line_group)
        self.assertIn("line-group rend-bold", block)
        self.assertIn("xml:id: verse-1", block)
        self.assertIn("resp: #VGA", block)
        self.assertIn("edRef: #Apte1929", block)

    def test_public_recipe_builds_bounded_nonrecursive_plan(self) -> None:
        config = load_config(REPOSITORY_ROOT / "collections.example.toml")
        collection = next(item for item in config.collections if item.id == "gretil")

        plan = plan_capture(config, "gretil")

        self.assertEqual(collection.recipe_api, 2)
        self.assertEqual(collection.analysis["validator_adapter"], "validator.py")
        self.assertEqual(collection.analysis["expected_work_count"], 801)
        self.assertEqual(collection.analysis["reader_adapter"], "reader.py")
        self.assertEqual(collection.analysis["reader_source"], "bulk-packages")
        self.assertFalse(hasattr(inventory, "write_static_reader"))
        self.assertTrue(callable(reader.write_static_reader))
        self.assertEqual(
            [source.id for source in collection.sources],
            ["current-register", "bulk-packages", "dictionaries", "frozen-register"],
        )
        self.assertEqual(len(plan.commands), 4)
        self.assertTrue(all("--max-redirect=0" in command.argv for command in plan.commands))
        self.assertTrue(all("--recursive" not in command.argv for command in plan.commands))
        bulk = next(command for command in plan.commands if command.source_id == "bulk-packages")
        self.assertEqual(len(bulk.argv[-8:]), 8)
        self.assertTrue(all(value.endswith(".zip") for value in bulk.argv[-8:]))
        for source in collection.sources:
            documented = (RECIPE_ROOT / f"seeds/{source.id}.txt").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(documented, list(source.seeds))

    def test_reviewed_identifier_fixture_matches_production_digest(self) -> None:
        identifiers = (RECIPE_ROOT / "fixtures/reviewed-tei-ids.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        digest = hashlib.sha256(("\n".join(identifiers) + "\n").encode()).hexdigest()

        self.assertEqual(len(identifiers), 801)
        self.assertEqual(digest, inventory.EXPECTED_TEI_ID_SHA256)


if __name__ == "__main__":
    unittest.main()
