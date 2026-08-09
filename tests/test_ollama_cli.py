from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class OllamaFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/api/tags":
            self.send_error(404)
            return
        self._reply({"models": [{"name": "fixture-editor:latest"}]})

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self.send_error(404)
            return
        length = int(self.headers["content-length"])
        request = json.loads(self.rfile.read(length))
        assert request["model"] == "fixture-editor:latest"
        assert request["stream"] is False
        assert request["think"] is False
        assert request["options"]["num_predict"] == 256
        assert request["format"] == {
            "type": "object",
            "properties": {"status": {"type": "string", "const": "ok"}},
            "required": ["status"],
            "additionalProperties": False,
        }
        self._reply({"message": {"role": "assistant", "content": '{"status":"ok"}'}})

    def _reply(self, value: object) -> None:
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.mark.contract
def test_ollama_check_requires_named_model_and_valid_structured_output() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                "epub-news-feeder",
                "ollama-check",
                "--host",
                f"http://127.0.0.1:{server.server_port}",
                "--model",
                "fixture-editor:latest",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert "code=OLLAMA_READY" in result.stdout
    assert "model=fixture-editor:latest" in result.stdout
    assert result.stderr == ""
