from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import json
import unittest

from kb_pipeline import PipelineConfig
from kb_pipeline.orchestrator import KnowledgeBasePipeline
from kb_pipeline.server import create_server, safe_document_path


def config_for(root: Path) -> PipelineConfig:
    config = PipelineConfig()
    config.storage.source_dir = str(root / "docs")
    config.storage.state_dir = str(root / "state")
    config.schedule.maintenance_start = "02:00"
    config.schedule.maintenance_end = "03:00"
    return config


class PipelineServerTest(unittest.TestCase):
    def test_customer_chat_and_operations_assets_are_available(self) -> None:
        with TemporaryDirectory() as tmp:
            pipeline = KnowledgeBasePipeline(config_for(Path(tmp)))
            server = create_server(pipeline, ("127.0.0.1", 0))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for path, expected in [("/", b"Aster"), ("/console.html", b"view-sessions"),
                                       ("/chat.js", b"/api/sessions/message"), ("/chat.css", b"prefers-reduced-motion"),
                                       ("/vendor/lucide.min.js", b"lucide"), ("/assets/chat-welcome.png", b"PNG")]:
                    with self.subTest(path=path), urlopen(base + path, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(expected, response.read())
                with urlopen(base + "/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                    self.assertIn('href="/console.html"', html)
                    self.assertNotIn("admin-token", html)
                    self.assertNotIn("https://", html)
            finally:
                server.shutdown()
                server.server_close()

    def test_safe_document_path_rejects_traversal(self) -> None:
        with TemporaryDirectory() as tmp:
            pipeline = KnowledgeBasePipeline(config_for(Path(tmp)))
            with self.assertRaises(ValueError):
                safe_document_path(pipeline, "../outside.md")

    def test_console_api_update_and_query(self) -> None:
        with TemporaryDirectory() as tmp:
            pipeline = KnowledgeBasePipeline(config_for(Path(tmp)))
            server = create_server(pipeline, ("127.0.0.1", 0))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                save = post(base + "/api/document", {"path": "policy.md", "content": "Refunds are available for 30 days with a receipt."})
                self.assertEqual(save["status"], "saved")
                update = post(
                    base + "/api/update",
                    {
                        "now": "2026-09-03T02:10:00",
                        "eval_cases": [
                            {
                                "question": "What is the refund window?",
                                "required_terms": ["30 days", "receipt"],
                                "source_hint": "refund",
                            }
                        ],
                    },
                )
                self.assertEqual(update["status"], "activated")
                query = post(
                    base + "/api/query",
                    {"token": "service-token", "question": "What is the refund window?"},
                )
                self.assertGreater(query["confidence"], 0)
                self.assertIn("30 days", query["answer"])
            finally:
                server.shutdown()
                server.server_close()

    def test_console_api_evidence_analysis(self) -> None:
        with TemporaryDirectory() as tmp:
            pipeline = KnowledgeBasePipeline(config_for(Path(tmp)))
            server = create_server(pipeline, ("127.0.0.1", 0))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                upload = post(
                    base + "/api/evidence/upload",
                    {
                        "filename": "invoice.pdf",
                        "content": "Order ID: ORD-1001\nDate: 2026-09-03\nTotal: $42.50\nProduct: Blue Widget",
                        "now": "2026-09-03T02:00:00",
                    },
                )
                result = post(
                    base + "/api/evidence/analyze",
                    {
                        "token": "service-token",
                        "message": "Order ID: ORD-1001. Date: 2026-09-03. Amount: $42.50. Product: Blue Widget.",
                        "file_refs": [upload["file_id"]],
                        "now": "2026-09-03T02:00:05",
                    },
                )
                self.assertEqual(result["status"], "complete")
            finally:
                server.shutdown()
                server.server_close()


def post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer admin-token"}, method="POST")
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
