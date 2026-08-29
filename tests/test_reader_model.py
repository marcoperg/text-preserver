from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from text_preserver.access.reader_model import (
    AccessArtifact,
    AccessCollection,
    AccessFacet,
    AccessItem,
    AccessRelation,
    AccessRepresentation,
    AccessSegment,
    access_document,
    access_collection_from_document,
    access_id,
    access_json,
    access_segment_json,
    load_access_collection,
    reader_model_identity,
    route_token,
    validate_access_indexes,
)


class ReaderModelTests(unittest.TestCase):
    def fixture(self) -> AccessCollection:
        collection_id = access_id("example", "collection", "")
        artifact_id = access_id("example", "artifact", "source.xml")
        item_id = access_id("example", "item", "work-1")
        representation_id = access_id("example", "representation", "work-1/text")
        segment_id = access_id("example", "segment", "work-1/line-1")
        return AccessCollection(
            collection_id,
            "Example",
            "complete",
            "index.html",
            (
                AccessItem(
                    item_id,
                    "Work 1",
                    "work",
                    "works/work-1.html",
                    "Example, Work 1.",
                    (
                        AccessRepresentation(
                            representation_id,
                            "Text",
                            "text",
                            "en",
                            "works/work-1.html#representation-text",
                            (artifact_id,),
                            (
                                AccessSegment(
                                    segment_id,
                                    "Line 1",
                                    "works/work-1.html#segment-line-1",
                                ),
                            ),
                        ),
                    ),
                    facets=(
                        AccessFacet(
                            "catalogue_category",
                            "Catalogue category",
                            ("Narrative literature",),
                        ),
                    ),
                ),
            ),
            (
                AccessArtifact(
                    artifact_id,
                    "Source XML",
                    "preservation_original",
                    "sources/source/mirror/source.xml",
                    "application/xml",
                    "0" * 64,
                ),
            ),
            (AccessRelation(item_id, "part_of", collection_id),),
            ("Collection rights vary.",),
        )

    def test_serializes_typed_access_graph_deterministically(self) -> None:
        document = access_document(self.fixture())
        encoded = access_json(self.fixture())

        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(
            document["items"][0]["facets"][0]["values"],
            ["Narrative literature"],
        )
        self.assertEqual(
            document["items"][0]["representations"][0]["segments"][0]["label"],
            "Line 1",
        )
        self.assertEqual(encoded, access_json(self.fixture()))
        self.assertIn('"preservation_original"', encoded)
        representation = self.fixture().items[0].representations[0]
        segment_line = access_segment_json(representation.id, representation.segments[0])
        self.assertTrue(segment_line.endswith("\n"))
        self.assertNotIn('": ', segment_line)
        self.assertNotIn('", ', segment_line)

    def test_rejects_unknown_artifacts_and_unsafe_routes(self) -> None:
        collection = self.fixture()
        representation = collection.items[0].representations[0]
        bad_representation = AccessRepresentation(
            representation.id,
            representation.label,
            representation.kind,
            representation.language,
            "../escape.html",
            ("tp:example/artifact/missing",),
        )
        bad_item = AccessItem(
            collection.items[0].id,
            collection.items[0].label,
            collection.items[0].item_type,
            collection.items[0].route,
            collection.items[0].citation,
            (bad_representation,),
        )

        with self.assertRaisesRegex(ValueError, "representation route"):
            access_document(
                AccessCollection(
                    collection.id,
                    collection.label,
                    collection.status,
                    collection.route,
                    (bad_item,),
                    collection.artifacts,
                )
            )

    def test_identifiers_and_route_tokens_are_portable(self) -> None:
        self.assertEqual(access_id("a b", "item", "x/y"), "tp:a%20b/item/x%2Fy")
        self.assertEqual(route_token("text name"), "text~20name")
        self.assertEqual(route_token("text~name"), "text~7ename")
        identity = reader_model_identity()
        self.assertEqual(identity["schema_version"], 2)
        self.assertRegex(str(identity["sha256"]), r"^[0-9a-f]{64}$")

    def test_rejects_remote_routes_and_noncanonical_artifact_paths(self) -> None:
        collection = self.fixture()
        remote_item = replace(collection.items[0], route="https://example.org/work")
        with self.assertRaisesRegex(ValueError, "item route"):
            access_document(replace(collection, items=(remote_item,)))
        encoded_item = replace(collection.items[0], route="works/%2e%2e/outside.html")
        with self.assertRaisesRegex(ValueError, "item route"):
            access_document(replace(collection, items=(encoded_item,)))

        unsafe_artifact = replace(
            collection.artifacts[0],
            capture_path="sources/source/../outside.xml",
        )
        with self.assertRaisesRegex(ValueError, "capture path"):
            access_document(replace(collection, artifacts=(unsafe_artifact,)))

        unsafe_member = replace(
            collection.artifacts[0],
            container_id=collection.artifacts[0].id,
            member_path="../../outside.xml",
        )
        with self.assertRaisesRegex(ValueError, "capture path"):
            access_document(replace(collection, artifacts=(unsafe_member,)))
        dot_artifact = replace(collection.artifacts[0], capture_path=".")
        with self.assertRaisesRegex(ValueError, "capture path"):
            access_document(replace(collection, artifacts=(dot_artifact,)))

    def test_rejects_duplicate_routes_and_artifact_container_cycles(self) -> None:
        collection = self.fixture()
        item = collection.items[0]
        duplicate = replace(item, id=access_id("example", "item", "work-2"))
        with self.assertRaisesRegex(ValueError, "duplicate item route"):
            access_document(replace(collection, items=(item, duplicate)))

        first_id = access_id("example", "artifact", "first.zip")
        second_id = access_id("example", "artifact", "second.zip")
        first = AccessArtifact(
            first_id,
            "First",
            "preservation_original",
            "sources/first.zip",
            "application/zip",
            container_id=second_id,
        )
        second = AccessArtifact(
            second_id,
            "Second",
            "preservation_original",
            "sources/second.zip",
            "application/zip",
            container_id=first_id,
        )
        with self.assertRaisesRegex(ValueError, "container cycle"):
            access_document(replace(collection, items=(), artifacts=(first, second)))

    def test_validates_external_segment_indexes_against_the_graph(self) -> None:
        collection = self.fixture()
        representation = collection.items[0].representations[0]
        segment = representation.segments[0]
        indexed = replace(
            representation,
            segments=(),
            segment_index="segments.jsonl",
        )
        indexed_collection = replace(
            collection,
            items=(replace(collection.items[0], representations=(indexed,)),),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "access.json").write_text(access_json(indexed_collection), encoding="utf-8")
            line = access_segment_json(indexed.id, segment)
            (root / "segments.jsonl").write_text(line, encoding="utf-8")

            validate_access_indexes(root)

            (root / "segments.jsonl").write_text(line + line, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate serialized"):
                validate_access_indexes(root)

    def test_strict_loader_rehydrates_only_canonical_access_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "access.json"
            path.write_text(access_json(self.fixture()), encoding="utf-8")

            loaded = load_access_collection(root)

            self.assertEqual(loaded, self.fixture())
            malformed = access_document(self.fixture())
            malformed["collection"]["unknown"] = True
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "collection fields"):
                load_access_collection(root)

    def test_strict_loader_preserves_canonical_schema_one_readers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = access_document(self.fixture())
            legacy["schema_version"] = 1
            for item in legacy["items"]:
                item.pop("facets")
            (root / "access.json").write_text(
                json.dumps(legacy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            loaded = load_access_collection(root)

            self.assertEqual(loaded.items[0].facets, ())

    def test_rejects_duplicate_and_inconsistent_facets(self) -> None:
        collection = self.fixture()
        item = collection.items[0]
        duplicate = replace(item, facets=item.facets + item.facets)
        with self.assertRaisesRegex(ValueError, "invalid item facet"):
            access_document(replace(collection, items=(duplicate,)))

        second = replace(
            item,
            id=access_id("example", "item", "work-2"),
            route="works/work-2.html",
            facets=(
                AccessFacet(
                    "catalogue_category",
                    "Different label",
                    ("Poetry",),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "inconsistent item facet label"):
            access_document(replace(collection, items=(item, second)))

        inconsistent_note = replace(
            second,
            facets=(
                AccessFacet(
                    "catalogue_category",
                    "Catalogue category",
                    ("Poetry",),
                    note="Different source qualification.",
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "inconsistent item facet note"):
            access_document(replace(collection, items=(item, inconsistent_note)))

        unknown_artifact = replace(
            item,
            facets=(
                AccessFacet(
                    "category",
                    "Category",
                    ("Poetry",),
                    ("tp:example/artifact/missing",),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "invalid item facet"):
            access_document(replace(collection, items=(unknown_artifact,)))

        malformed = access_document(collection)
        malformed["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "unsupported access model schema"):
            access_collection_from_document(malformed)

    def test_strict_loader_rejects_hardlinked_access_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(access_json(self.fixture()), encoding="utf-8")
            (root / "reader").mkdir()
            (root / "reader/access.json").hardlink_to(source)

            with self.assertRaisesRegex(ValueError, "bounded regular file"):
                load_access_collection(root / "reader")


if __name__ == "__main__":
    unittest.main()
