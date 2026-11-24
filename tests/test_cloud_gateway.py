"""OAuth boundary checks; no Google credentials or network requests are needed."""

import asyncio
import json
import os
import time
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
            "SCRIBEFLOW_DRIVE_JOB_KEY": "set-for-test",
        }

    def server(self, **settings):
        from key_value.aio.stores.memory import MemoryStore
        with patch.dict(os.environ, {**self.settings, **settings}, clear=True):
            with patch("scribeflow.cloud_gateway.CloudRunJobs"):
                with patch("key_value.aio.stores.firestore.FirestoreStore",
                           side_effect=lambda **kwargs: MemoryStore()):
                    return create_server()

    def test_google_config_required(self):
        for key in ("SCRIBEFLOW_MCP_URL", "SCRIBEFLOW_GOOGLE_EMAIL",
                    "SCRIBEFLOW_GOOGLE_CLIENT_ID", "SCRIBEFLOW_GOOGLE_CLIENT_SECRET",
                    "SCRIBEFLOW_OAUTH_SEED", "SCRIBEFLOW_DRIVE_JOB_KEY"):
            with self.subTest(missing=key):
                settings = {**self.settings}
                settings.pop(key)
                with patch.dict(os.environ, settings, clear=True):
                    with self.assertRaises((KeyError, ValueError)):
                        create_server()

    def test_automatic_upgrade_and_explicit_replacement_are_marked_destructive(self):
        server = self.server()
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(set(tools), {"get_profile", "get_google_profile_photo", "search_drive_files",
                                    "list_google_shared_drives", "check_drive_file", "transcribe_audio",
                                    "retranscribe_audio", "get_transcription_status"})
        self.assertEqual(tools["get_profile"].meta["openai/profile"], True)
        self.assertTrue(tools["transcribe_audio"].annotations.destructive_hint)
        self.assertTrue(tools["retranscribe_audio"].annotations.destructive_hint)
        self.assertIn("transcript_file_id", tools["retranscribe_audio"].parameters["properties"])

    def test_transcribe_tool_exposes_optional_context_fields(self):
        tools = {
            tool.name: tool for tool in asyncio.run(self.server().list_tools())
        }
        schema = tools["transcribe_audio"].parameters
        self.assertIn("file_id", schema["properties"])
        self.assertIn("keywords", schema["properties"])
        self.assertIn("prompt", schema["properties"])
        self.assertIn("retry", schema["properties"])
        self.assertIn("language", schema["properties"])
        for name in ("transcribe_audio", "retranscribe_audio"):
            tool = tools[name]
            output_format = tool.parameters["properties"]["output_format"]
            self.assertEqual(output_format["enum"], ["txt", "md"])
            self.assertEqual(output_format["default"], "txt")
            self.assertNotIn("output_format", tool.parameters.get("required", []))
            self.assertIn("'txt'", tool.description)
            self.assertIn("'md'", tool.description)
        self.assertIn("file_id", schema.get("required", []))
        self.assertNotIn("keywords", schema.get("required", []))
        self.assertNotIn("prompt", schema.get("required", []))
        self.assertNotIn("retry", schema.get("required", []))
        self.assertNotIn("language", schema.get("required", []))

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

    def test_linked_profile_uses_current_account_without_a_nickname(self):
        from fastmcp import Client
        from fastmcp.server.auth import AccessToken

        server = self.server(
            SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS="second@example.com",
        )

        async def check():
            async with Client(server) as client:
                for email, subject in (("owner@example.com", "google-1"), ("second@example.com", "google-2")):
                    claims = {
                        "email": email, "sub": subject, "email_verified": True, "name": "Google user",
                        "picture": "https://example.com/profile.jpg",
                    }
                    with patch("fastmcp.server.dependencies.get_access_token",
                               return_value=AccessToken(token="fake", client_id=subject, scopes=[], claims=claims)):
                        profile = await client.call_tool("get_profile", {})
                        photo = await client.call_tool("get_google_profile_photo", {})
                    self.assertEqual(profile.structured_content, {
                        "id": subject, "email": email, "name": "Google user",
                    })
                    self.assertEqual(json.loads(profile.content[0].text), profile.structured_content)
                    self.assertEqual(photo.structured_content, {"picture": claims["picture"]})

        asyncio.run(check())

    def test_transcribe_uses_the_selected_accounts_drive_grant(self):
        from cryptography.fernet import Fernet
        from fastmcp import Client
        from fastmcp.server.auth import AccessToken

        from scribeflow.linked_drive import DRIVE_SCOPE, DriveCredential

        key = Fernet.generate_key().decode()
        server = self.server(
            SCRIBEFLOW_DRIVE_JOB_KEY=key,
            SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS="second@example.com third@example.com",
        )
        file_id = "audio_" + "a" * 26
        submitted = {"status": "submitted", "operation": "existing-test-operation"}

        async def check():
            async with Client(server) as client:
                for index, email in enumerate(("owner@example.com", "second@example.com", "third@example.com")):
                    subject = f"google-{index}"
                    grant = AccessToken(
                        token=f"account-token-{index}", client_id=subject, scopes=[DRIVE_SCOPE],
                        expires_at=int(time.time()) + 3600,
                        claims={"sub": subject, "email": email, "email_verified": True},
                    )
                    with self.subTest(email=email), patch.dict(os.environ, {"SCRIBEFLOW_DRIVE_JOB_KEY": key}), \
                         patch("fastmcp.server.dependencies.get_access_token", return_value=grant), \
                         patch("scribeflow.cloud_gateway.check_file", return_value={
                             "name": "audio.wav", "can_download": True, "can_save_transcript": True,
                         }) as check_file, \
                         patch("scribeflow.cloud_gateway.DeduplicatingCloudRunJobs.start",
                               return_value=submitted) as launch:
                        result = await client.call_tool("transcribe_audio", {"file_id": file_id})
                        self.assertEqual(result.structured_content, submitted)
                        check_file.assert_called_once()
                        self.assertEqual(check_file.call_args.args[0].access_token, grant.token)
                        launch.assert_called_once()
                        self.assertEqual(launch.call_args.kwargs["owner"], subject)
                        envelope = launch.call_args.kwargs["drive_envelope"]
                        self.assertEqual(DriveCredential.open(envelope, file_id).access_token, grant.token)

        asyncio.run(check())

    def test_linked_replacement_keeps_explicit_transcript_id_and_markdown_format(self):
        from cryptography.fernet import Fernet
        from fastmcp import Client
        from fastmcp.server.auth import AccessToken

        key = Fernet.generate_key().decode()
        server = self.server(SCRIBEFLOW_DRIVE_JOB_KEY=key)
        audio_id = "audio_" + "a" * 25
        text_id = "transcript_" + "b" * 25

        async def check():
            async with Client(server) as client:
                grant = AccessToken(
                    token="fake", client_id="owner-id",
                    scopes=["https://www.googleapis.com/auth/drive"],
                    expires_at=int(time.time()) + 3600,
                    claims={"sub": "owner-id", "email": "owner@example.com", "email_verified": True},
                )
                with patch.dict(os.environ, {"SCRIBEFLOW_DRIVE_JOB_KEY": key}), \
                     patch("fastmcp.server.dependencies.get_access_token", return_value=grant), \
                     patch("scribeflow.cloud_gateway.check_file", return_value={
                         "name": "audio.m4a", "can_download": True,
                         "can_save_transcript": False,
                     }), \
                     patch("scribeflow.cloud_gateway.DeduplicatingCloudRunJobs.start",
                           return_value={"status": "submitted"}) as submit:
                    await client.call_tool("retranscribe_audio", {
                        "file_id": audio_id, "transcript_file_id": text_id,
                        "output_format": "md",
                    })
                    self.assertEqual(submit.call_args.kwargs["transcript_file_id"], text_id)
                    self.assertEqual(submit.call_args.kwargs["output_format"], "md")
        asyncio.run(check())

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
