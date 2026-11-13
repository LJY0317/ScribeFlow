import asyncio
import os
import unittest
from unittest.mock import patch

from scribeflow.cloud_gateway import create_server


_TOKEN = "s" * 48


class CloudGatewayTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "SCRIBEFLOW_MCP_ACCESS_TOKEN": _TOKEN,
            "GOOGLE_CLOUD_PROJECT": "sample-cloud-project",
        }

    def test_missing_or_weak_access_token_rejected(self):
        for secret in ("", "password", "not a secret", "x" * 500):
            with self.subTest(length=len(secret)):
                with patch.dict(os.environ, {**self.settings, "SCRIBEFLOW_MCP_ACCESS_TOKEN": secret}):
                    with self.assertRaises(ValueError):
                        create_server()

    def test_only_one_audio_tool_and_status_tool(self):
        with patch.dict(os.environ, self.settings):
            with patch("scribeflow.cloud_gateway.CloudRunJobs"):
                server = create_server()
        tools = asyncio.run(server.list_tools())
        self.assertEqual({tool.name for tool in tools},
                         {"transcribe_audio", "get_transcription_status"})
        self.assertEqual(server.settings.streamable_http_path, f"/{_TOKEN}/mcp")

    def test_known_url_required_for_http_mcp(self):
        import httpx

        with patch.dict(os.environ, self.settings):
            with patch("scribeflow.cloud_gateway.CloudRunJobs"):
                server = create_server()
        app = server.streamable_http_app()

        async def verify():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost",
                ) as client:
                    request = {
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1.0"}},
                    }
                    headers = {"Accept": "application/json, text/event-stream"}
                    wrong = await client.post("/mcp", json=request, headers=headers)
                    self.assertEqual(wrong.status_code, 404)
                    right = await client.post(f"/{_TOKEN}/mcp", json=request, headers=headers)
                    self.assertEqual(right.status_code, 200)
                    self.assertEqual(right.json()["result"]["serverInfo"]["name"], "ScribeFlow")

        asyncio.run(verify())


if __name__ == "__main__":
    unittest.main()
