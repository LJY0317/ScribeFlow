"""Authenticated, stateless remote MCP entrypoint for Cloud Run."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from .cloud_run import CloudRunJobs, CloudRunTarget


class VerifiedOAuthToken:
    """Validate Auth0 JWT signature, issuer, audience, scope and user identity."""

    def __init__(self, issuer: str, audience: str, allowed_subject: str, scope: str):
        import jwt
        self.jwt = jwt
        self.issuer = issuer if issuer.endswith("/") else issuer + "/"
        self.audience = audience
        self.allowed_subject = allowed_subject
        self.scope = scope
        self.jwks = jwt.PyJWKClient(self.issuer + ".well-known/jwks.json")

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = self.jwt.decode(
                token, key, algorithms=["RS256"],
                issuer=self.issuer, audience=self.audience,
                options={"require": ["iss", "aud", "exp", "sub"]},
            )
        except (self.jwt.PyJWTError, OSError, ValueError):
            return None
        scopes = str(claims.get("scope", "")).split()
        if claims.get("sub") != self.allowed_subject or self.scope not in scopes:
            return None
        return AccessToken(
            token=token, client_id=claims.get("azp", "oauth-client"),
            scopes=scopes, expires_at=int(claims["exp"]),
            resource=self.audience, subject=claims["sub"], claims=claims,
        )


def create_server():
    """Fail closed: the server never exposes paid tools without OAuth config."""
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    url = os.environ.get("SCRIBEFLOW_MCP_URL", "").rstrip("/")
    issuer = os.environ.get("SCRIBEFLOW_OAUTH_ISSUER", "")
    subject = os.environ.get("SCRIBEFLOW_ALLOWED_SUBJECT", "")
    scope = "transcribe:write"
    if not url.startswith("https://") or urlparse(url).path not in {"", "/"}:
        raise ValueError("SCRIBEFLOW_MCP_URL must be the HTTPS service origin")
    if not issuer.startswith("https://") or not subject:
        raise ValueError("OAuth issuer and allowed subject must be configured")
    resource_url = url + "/mcp"
    verifier = VerifiedOAuthToken(issuer, resource_url, subject, scope)
    backend = CloudRunJobs(CloudRunTarget(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        region=os.environ.get("SCRIBEFLOW_REGION", "asia-northeast3"),
        job=os.environ.get("SCRIBEFLOW_JOB", "scribeflow-transcribe"),
    ))
    server = FastMCP(
        "ScribeFlow", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
        stateless_http=True, json_response=True,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=issuer, resource_server_url=resource_url,
            required_scopes=[scope], validate_token_resource=True,
        ),
    )
    oauth = {"securitySchemes": [{"type": "oauth2", "scopes": [scope]}]}

    @server.tool(
        description=(
            "Transcribe a specifically requested Google Drive audio file. "
            "Uses the paid OpenAI transcription API; never batch-submit music."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    openWorldHint=False),
        meta=oauth,
    )
    def transcribe_audio(file_id: str) -> dict:
        """Start one transcription; returns an operation to check later."""
        return backend.start(file_id)

    @server.tool(
        description="Check an existing ScribeFlow transcription without starting another job.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        meta=oauth,
    )
    def get_transcription_status(operation: str) -> dict:
        """Get pending/completed/failed status for a submitted execution."""
        return backend.status(operation)

    return server


def main() -> None:
    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
