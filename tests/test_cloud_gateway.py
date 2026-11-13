"""OAuth boundary checks; no Google credentials or network requests are needed."""

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scribeflow.cloud_gateway import _require_owner, create_server


class CloudGatewayTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "SCRIBEFLOW_MCP_URL": "https://scribeflow-mcp-123.asia-northeast3.run.app",
            "SCRIBEFLOW_GOOGLE_EMAIL": "owner@example.com",
            "SCRIBEFLOW_GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
            "SCRIBEFLOW_GOOGLE_CLIENT_SECRET": "test-secret",
            "SCRIBEFLOW_OAUTH_SEED": "s" * 64,
            "GOOGLE_CLOUD_PROJECT": "sample-cloud-project",
        }

    def server(self):
        from key_value.aio.stores.memory import MemoryStore
        with patch.dict(os.environ, self.settings):
            with patch("scribeflow.cloud_gateway.CloudRunJobs"):
                with patch("key_value.aio.stores.firestore.FirestoreStore",
                           side_effect=lambda **kwargs: MemoryStore()):
                    return create_server()

    def test_google_config_required(self):
        for key in ("SCRIBEFLOW_MCP_URL", "SCRIBEFLOW_GOOGLE_EMAIL",
                    "SCRIBEFLOW_GOOGLE_CLIENT_ID", "SCRIBEFLOW_GOOGLE_CLIENT_SECRET",
                    "SCRIBEFLOW_OAUTH_SEED"):
            with self.subTest(missing=key):
                settings = {**self.settings}
                settings.pop(key)
                with patch.dict(os.environ, settings, clear=True):
                    with self.assertRaises((KeyError, ValueError)):
                        create_server()

    def test_only_two_tools(self):
        server = self.server()
        self.assertEqual({tool.name for tool in asyncio.run(server.list_tools())},
                         {"transcribe_audio", "get_transcription_status"})

    def test_transcribe_tool_exposes_optional_context_fields(self):
        tools = {
            tool.name: tool for tool in asyncio.run(self.server().list_tools())
        }
        schema = tools["transcribe_audio"].parameters
        self.assertIn("file_id", schema["properties"])
        self.assertIn("keywords", schema["properties"])
        self.assertIn("prompt", schema["properties"])
        self.assertIn("file_id", schema.get("required", []))
        self.assertNotIn("keywords", schema.get("required", []))
        self.assertNotIn("prompt", schema.get("required", []))

    def test_owner_verified_google_email_required(self):
        owner = "owner@example.com"
        claims = {"email": owner, "email_verified": True, "sub": "google-123"}
        with patch("fastmcp.server.dependencies.get_access_token",
                   return_value=SimpleNamespace(claims=claims)):
            _require_owner(owner)
        for other in (
            None,
            {**claims, "email": "other@example.com"},
            {**claims, "email_verified": False},
            {**claims, "sub": None},
        ):
            with self.subTest(claims=other):
                with patch("fastmcp.server.dependencies.get_access_token",
                           return_value=SimpleNamespace(claims=other) if other else None):
                    with self.assertRaises(PermissionError):
                        _require_owner(owner)

    def test_oauth_metadata_and_unauthorized_mcp(self):
        import httpx2 as httpx
        server = self.server()
        app = server.http_app(stateless_http=True)

        async def check():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://scribeflow-mcp-123.asia-northeast3.run.app",
                ) as client:
                    metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
                    self.assertEqual(metadata.status_code, 200)
                    anonymous = await client.post("/mcp", json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    }, headers={"Accept": "application/json, text/event-stream"})
                    self.assertEqual(anonymous.status_code, 401)
                    registration = await client.post("/register", json={
                        "client_name": "ChatGPT",
                        "redirect_uris": ["https://chatgpt.com/connector/oauth/callback"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    })
                    self.assertEqual(registration.status_code, 201)
                    self.assertTrue(registration.json().get("client_id"))

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
