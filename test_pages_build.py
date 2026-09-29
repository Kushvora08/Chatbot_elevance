from html.parser import HTMLParser
from pathlib import Path
import unittest

from scripts.build_pages import build, demo_data

ROOT = Path(__file__).resolve().parents[1]


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "src" in attrs:
            self.references.append(attrs["src"])
        if tag == "link" and "href" in attrs:
            self.references.append(attrs["href"])


class PagesBuildTests(unittest.TestCase):
    def test_committed_preview_is_up_to_date(self):
        self.assertEqual((ROOT / "index.html").read_text(encoding="utf-8"), build())

    def test_assets_are_embedded_and_dom_ids_are_unique(self):
        parser = AssetParser()
        parser.feed(build())
        self.assertTrue(parser.references)
        self.assertTrue(all(ref.startswith("data:") for ref in parser.references))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))

    def test_only_public_sample_data_and_no_server_credentials(self):
        self.assertEqual(len(demo_data()["documents"]), 6)
        self.assertTrue(all(d["metadata"]["access_level"] == "public" for d in demo_data()["documents"]))
        page = build()
        for secret in ("service-token", "admin-token", "Preview-only-2026", "flask_session"):
            self.assertNotIn(secret, page)
        self.assertIn("connect-src 'none'", page)

    def test_publish_artifact_is_not_the_repository(self):
        workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn("cp index.html _site/index.html", workflow)
        self.assertIn("path: _site", workflow)


if __name__ == "__main__":
    unittest.main()
