import asyncio
import os
import time
import unittest
from unittest.mock import patch

from scribeflow.cloud_gateway import VerifiedOAuthToken, create_server


class CloudGatewayTests(unittest.TestCase):
    def test_boot_requires_oauth_config(self):
        with patch.dict(os.environ, {
            "SCRIBEFLOW_MCP_URL": "",
            "SCRIBEFLOW_OAUTH_ISSUER": "",
            "SCRIBEFLOW_ALLOWED_SUBJECT": "",
        }):
            with self.assertRaises(ValueError):
                create_server()

    def test_authenticated_tools_are_registered(self):
        settings = {
            "SCRIBEFLOW_MCP_URL": "https://scribeflow-example.a.run.app",
            "SCRIBEFLOW_OAUTH_ISSUER": "https://example.us.auth0.com/",
            "SCRIBEFLOW_ALLOWED_SUBJECT": "google-oauth2|example",
            "GOOGLE_CLOUD_PROJECT": "sample-cloud-project",
            "SCRIBEFLOW_DEFAULT_FOLDER_ID": "folder_" + "a" * 25,
            "SCRIBEFLOW_ALLOWED_FOLDER_IDS": "",
        }
        with patch.dict(os.environ, settings):
            with patch("scribeflow.cloud_gateway.CloudRunJobs"):
                server = create_server()
        tools = asyncio.run(server.list_tools())
        self.assertEqual(
            {tool.name for tool in tools},
            {"transcribe_audio", "get_transcription_status"},
        )
        for tool in tools:
            self.assertEqual(tool.meta["securitySchemes"][0]["type"], "oauth2")

    def test_entire_drive_works_without_folder_allowlist(self):
        settings = {
            "SCRIBEFLOW_MCP_URL": "https://scribeflow-example.a.run.app",
            "SCRIBEFLOW_OAUTH_ISSUER": "https://example.us.auth0.com/",
            "SCRIBEFLOW_ALLOWED_SUBJECT": "google-oauth2|example",
            "GOOGLE_CLOUD_PROJECT": "sample-cloud-project",
            "SCRIBEFLOW_DEFAULT_FOLDER_ID": "",
            "SCRIBEFLOW_ALLOWED_FOLDER_IDS": "",
        }
        with patch.dict(os.environ, settings):
            with patch("scribeflow.cloud_gateway.CloudRunJobs") as jobs:
                create_server()
        self.assertEqual(jobs.call_args.kwargs["allowed_folder_ids"], ())

    def test_http_mcp_rejects_anonymous_calls(self):
        import httpx

        settings = {
            "SCRIBEFLOW_MCP_URL": "https://scribeflow-example.a.run.app",
            "SCRIBEFLOW_OAUTH_ISSUER": "https://example.us.auth0.com/",
            "SCRIBEFLOW_ALLOWED_SUBJECT": "google-oauth2|example",
            "GOOGLE_CLOUD_PROJECT": "sample-cloud-project",
            "SCRIBEFLOW_DEFAULT_FOLDER_ID": "folder_" + "a" * 25,
            "SCRIBEFLOW_ALLOWED_FOLDER_IDS": "",
        }
        with patch.dict(os.environ, settings):
            with patch("scribeflow.cloud_gateway.CloudRunJobs"):
                server = create_server()
        app = server.streamable_http_app()

        async def verify():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
            ) as client:
                metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
                self.assertEqual(metadata.status_code, 200)
                self.assertEqual(
                    metadata.json()["resource"],
                    "https://scribeflow-example.a.run.app/mcp",
                )
                unauthenticated = await client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                })
                self.assertEqual(unauthenticated.status_code, 401)
                self.assertIn("resource_metadata=", unauthenticated.headers["www-authenticate"])

        asyncio.run(verify())

    def test_jwt_verifies_signature_audience_scopes_and_subject(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        import jwt

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        verifier = VerifiedOAuthToken(
            "https://example.us.auth0.com/",
            "https://scribeflow-example.a.run.app/mcp",
            "google-oauth2|owner",
            "transcribe:write",
        )
        verifier.jwks.get_signing_key_from_jwt = lambda token: type("Key", (), {
            "key": private.public_key()
        })()
        claims = {
            "iss": "https://example.us.auth0.com/",
            "aud": "https://scribeflow-example.a.run.app/mcp",
            "sub": "google-oauth2|owner",
            "scope": "transcribe:write",
            "exp": int(time.time()) + 120,
        }

        def token(overrides=None, sign_key=private):
            return jwt.encode(
                {**claims, **(overrides or {})},
                sign_key, algorithm="RS256", headers={"kid": "test"},
            )

        self.assertIsNotNone(asyncio.run(verifier.verify_token(token())))
        self.assertIsNone(asyncio.run(verifier.verify_token(
            token({"sub": "google-oauth2|stranger"})
        )))
        self.assertIsNone(asyncio.run(verifier.verify_token(
            token({"aud": "https://another-service.example/mcp"})
        )))
        self.assertIsNone(asyncio.run(verifier.verify_token(
            token({"scope": "read:only"})
        )))
        self.assertIsNone(asyncio.run(verifier.verify_token(
            token({"exp": int(time.time()) - 120})
        )))
        wrong_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertIsNone(asyncio.run(verifier.verify_token(
            token(sign_key=wrong_private)
        )))


if __name__ == "__main__":
    unittest.main()
