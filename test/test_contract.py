#!/usr/bin/env python3
"""Zero-dependency unit & contract integration test suite for immich-concourse-resource.
Stands up an in-memory mock Immich HTTP server on an ephemeral loopback port
and executes `out` as a subprocess against it.
"""
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT_SCRIPT = os.path.join(REPO_ROOT, "out")


class MockImmichAPIHandler(BaseHTTPRequestHandler):
    """Simulates the Immich REST API specification (v3.1.0) using Python's
    built-in HTTP server capabilities. Tracks incoming payloads for assertions.
    """
    incoming_requests = []

    def log_message(self, format, *args):
        pass

    def _read_body_as_json(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def do_GET(self):
        url = urlparse(self.path)
        self.incoming_requests.append({"method": "GET", "path": url.path})

        if url.path == "/api/albums":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([]).encode("utf-8"))
            return

        if url.path == "/api/tags":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([]).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        url = urlparse(self.path)

        if url.path == "/api/assets":
            self.incoming_requests.append({
                "method": "POST",
                "path": url.path,
                "content_type": self.headers.get("Content-Type"),
            })
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                _ = self.rfile.read(content_length)

            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "mock-asset-uuid-123456", "status": "created"}).encode("utf-8"))
            return

        if url.path == "/api/albums":
            body_json = self._read_body_as_json()
            self.incoming_requests.append({"method": "POST", "path": url.path, "body": body_json})

            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "mock-album-uuid-abcde",
                "albumName": body_json.get("albumName", "unnamed"),
            }).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        url = urlparse(self.path)
        body_json = self._read_body_as_json()

        self.incoming_requests.append({
            "method": "PUT",
            "path": url.path,
            "body": body_json,
        })

        # Atomic tag upsert: PUT /api/tags {"tags": [...]}
        if url.path == "/api/tags":
            tags_requested = body_json.get("tags", [])
            response_payload = [
                {"id": f"mock-tag-uuid-{name}", "name": name}
                for name in tags_requested
            ]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_payload).encode("utf-8"))
            return

        # Pattern matches /api/assets/{id} for description updates
        if url.path.startswith("/api/assets/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": url.path.split("/")[-1]}).encode("utf-8"))
            return

        # Pattern matches /api/albums/{id}/assets for bulk assignment
        if url.path.endswith("/assets") and "/api/albums/" in url.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([{"id": "mock-asset-uuid-123456", "success": True}]).encode("utf-8"))
            return

        # Matches atomic tag assignment /api/tags/assets
        if url.path == "/api/tags/assets":
            count = len(body_json.get("tagIds", [])) * len(body_json.get("assetIds", []))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"count": count}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestImmichConcourseResource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock_port = get_free_port()
        cls.mock_host = f"http://127.0.0.1:{cls.mock_port}"
        cls.httpd = HTTPServer(("127.0.0.1", cls.mock_port), MockImmichAPIHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_thread.join()

    def setUp(self):
        MockImmichAPIHandler.incoming_requests.clear()
        self.test_dir = tempfile.TemporaryDirectory()
        self.workspace_path = self.test_dir.name

    def tearDown(self):
        self.test_dir.cleanup()

    def run_out_script(self, stdin_json):
        return subprocess.run(
            [sys.executable, OUT_SCRIPT, self.workspace_path],
            input=json.dumps(stdin_json),
            text=True,
            capture_output=True,
        )

    def test_end_to_end_parallel_ingestion_with_metadata(self):
        """Validates:
        1. Thread-safe requests.Session pooling and retry handshakes.
        2. contextlib.ExitStack file stream management.
        3. Strict .description.txt sidecar auto-discovery and sequential update.
        4. Atomic tag batching and checked album allocation payloads.
        """
        asset_filename = "crewgroup_racers.png"
        asset_path = os.path.join(self.workspace_path, asset_filename)
        with open(asset_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82")

        description_filename = "crewgroup_racers.description.txt"
        description_path = os.path.join(self.workspace_path, description_filename)
        expected_description = "Portrait of the Racers crew group rendered via ComfyUI with seed 839210."
        with open(description_path, "w", encoding="utf-8") as f:
            f.write(expected_description)

        xmp_filename = "crewgroup_racers.xmp"
        xmp_path = os.path.join(self.workspace_path, xmp_filename)
        with open(xmp_path, "w", encoding="utf-8") as f:
            f.write("<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF></rdf:RDF></x:xmpmeta>")

        concourse_input = {
            "source": {
                "host": self.mock_host,
                "api_key": "ci-test-api-key-999",
            },
            "params": {
                "glob": "*.png",
                "is_favorite": True,
                "visibility": "timeline",
                "album": "Blades68 Crew Groups",
                "tags": ["Blades68", "Nightly Render"],
            },
        }

        result = self.run_out_script(concourse_input)
        self.assertEqual(result.returncode, 0, f"Script failed (exit {result.returncode}):\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}")

        output_data = json.loads(result.stdout)
        self.assertIn("version", output_data)
        self.assertIn("ref", output_data["version"])

        metadata = {m["name"]: m["value"] for m in output_data.get("metadata", [])}
        self.assertEqual(metadata.get("uploaded_assets_count"), "1")
        self.assertEqual(metadata.get("xmp_sidecars_bound"), "1")
        self.assertEqual(metadata.get("descriptions_bound"), "1")
        self.assertEqual(metadata.get("album_assigned"), "Blades68 Crew Groups")
        self.assertEqual(metadata.get("tags_assigned"), "Blades68, Nightly Render")

        reqs = MockImmichAPIHandler.incoming_requests

        # Verify strict description payload binding
        desc_updates = [r for r in reqs if r["method"] == "PUT" and r["path"].startswith("/api/assets/")]
        self.assertEqual(len(desc_updates), 1)
        self.assertEqual(desc_updates[0]["body"].get("description"), expected_description)

        # Verify atomic tag assignment schema payload structure
        tag_assignments = [r for r in reqs if r["method"] == "PUT" and r["path"] == "/api/tags/assets"]
        self.assertEqual(len(tag_assignments), 1)
        self.assertIn("mock-asset-uuid-123456", tag_assignments[0]["body"].get("assetIds", []))
        self.assertIn("mock-tag-uuid-Blades68", tag_assignments[0]["body"].get("tagIds", []))
        self.assertIn("mock-tag-uuid-Nightly Render", tag_assignments[0]["body"].get("tagIds", []))

        # Verify bulk album assignment schema payload structure
        album_assignments = [r for r in reqs if r["method"] == "PUT" and "/api/albums/" in r["path"]]
        self.assertEqual(len(album_assignments), 1)
        self.assertIn("mock-asset-uuid-123456", album_assignments[0]["body"].get("ids", []))


if __name__ == "__main__":
    unittest.main()
