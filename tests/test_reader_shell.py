from __future__ import annotations

import unittest

from text_preserver.access.reader_shell import (
    ReaderFact,
    ReaderLink,
    reader_shell_identity,
    reader_stylesheet,
    render_artifact_reference,
    render_citation,
    render_document,
    render_facts,
    render_navigation,
    render_notice,
    render_status,
)


class ReaderShellTests(unittest.TestCase):
    def test_renders_inert_external_asset_document(self) -> None:
        document = render_document(
            "A <title>",
            "<main>Recipe body</main>",
            asset_prefix="../",
            collection_stylesheet="collection.css",
        )

        self.assertIn("A &lt;title&gt;", document)
        self.assertIn("default-src 'none'; style-src 'self'", document)
        self.assertIn('href="../assets/reader.css"', document)
        self.assertIn('href="../assets/collection.css"', document)
        self.assertNotIn("<style", document)
        self.assertNotIn("<script", document)

    def test_components_escape_recipe_text_and_reject_remote_links(self) -> None:
        navigation = render_navigation(
            (ReaderLink("Catalogue <root>", "../index.html"),),
            next_=ReaderLink("Next & item", "next.html#segment-1"),
        )
        facts = render_facts((ReaderFact("Source <path>", "a&b.xml"),))
        status = render_status("incomplete", ("Missing <record>",))
        artifact = render_artifact_reference(
            "Source <artifact>",
            "tp:example/artifact/source&one",
            "../access.json",
        )
        citation = render_citation("Work <one>", "tp:example/item/one&two")

        self.assertIn("Catalogue &lt;root&gt;", navigation)
        self.assertIn("Next: Next &amp; item", navigation)
        self.assertIn("Source &lt;path&gt;", facts)
        self.assertIn("a&amp;b.xml", facts)
        self.assertIn("Missing &lt;record&gt;", status)
        self.assertIn("Source &lt;artifact&gt;", artifact)
        self.assertIn("tp:example/artifact/source&amp;one", artifact)
        self.assertIn("Work &lt;one&gt;", citation)
        self.assertIn("tp:example/item/one&amp;two", citation)
        self.assertEqual(render_notice("Source <notice>"), '<aside class="reader-notice">Source &lt;notice&gt;</aside>')
        with self.assertRaisesRegex(ValueError, "not local"):
            render_navigation((ReaderLink("Remote", "https://example.org/"),))

    def test_identity_and_stylesheet_are_stable_inputs(self) -> None:
        identity = reader_shell_identity()

        self.assertEqual(identity["schema_version"], 1)
        self.assertRegex(str(identity["sha256"]), r"^[0-9a-f]{64}$")
        self.assertIn(".reader-nav", reader_stylesheet())
        self.assertIn(".reader-status", reader_stylesheet())


if __name__ == "__main__":
    unittest.main()
