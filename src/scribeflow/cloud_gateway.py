"""Google-account OAuth protection for the remote ScribeFlow MCP service."""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from .cloud_dedup import DeduplicatingCloudRunJobs, FirestoreJobClaims
from .cloud_run import CloudRunJobs, CloudRunTarget


def _require_owner(allowed_email: str) -> None:
    """Prevent other Google accounts from triggering paid or Drive operations."""
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    claims = token.claims if token else {}
    verified = claims.get("email_verified")
    if (
        not token
        or (claims.get("email") or "").casefold() != allowed_email.casefold()
        or verified not in (True, "true", "True", "1")
        or not claims.get("sub")
    ):
        raise PermissionError("This Google account is not authorized for ScribeFlow")


def create_server():
    from cryptography.fernet import Fernet
    from fastmcp import FastMCP
    from fastmcp.server.auth.providers.google import GoogleProvider
    from key_value.aio.stores.firestore import (
        FirestoreStore,
        FirestoreV1CollectionSanitizationStrategy,
        FirestoreV1KeySanitizationStrategy,
    )
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from mcp.types import ToolAnnotations

    # Cloud Run scales to zero; OAuth registrations and tokens must persist.
    origin = os.environ["SCRIBEFLOW_MCP_URL"].rstrip("/")
    owner_email = os.environ["SCRIBEFLOW_GOOGLE_EMAIL"].strip()
    client_id = os.environ["SCRIBEFLOW_GOOGLE_CLIENT_ID"]
    client_secret = os.environ["SCRIBEFLOW_GOOGLE_CLIENT_SECRET"]
    seed = os.environ["SCRIBEFLOW_OAUTH_SEED"]
    if not origin.startswith("https://") or not owner_email or len(seed) < 40:
        raise ValueError("Google OAuth configuration is incomplete")
    material = seed.encode("utf-8")
    jwt_key = hashlib.sha256(b"scribeflow-jwt-v1:" + material).digest()
    fernet_key = base64.urlsafe_b64encode(
        hashlib.sha256(b"scribeflow-storage-v1:" + material).digest()
    )
    store = FernetEncryptionWrapper(
        key_value=FirestoreStore(
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            database="scribeflow-oauth",
            default_collection="scribeflow_oauth",
            key_sanitization_strategy=FirestoreV1KeySanitizationStrategy(),
            collection_sanitization_strategy=FirestoreV1CollectionSanitizationStrategy(),
        ),
        fernet=Fernet(fernet_key),
    )
    auth = GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=origin,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        jwt_signing_key=jwt_key,
        client_storage=store,
        require_authorization_consent=True,
    )
    jobs = CloudRunJobs(CloudRunTarget(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        region=os.environ.get("SCRIBEFLOW_REGION", "asia-northeast3"),
        job=os.environ.get("SCRIBEFLOW_JOB", "scribeflow-transcribe"),
    ))
    backend = DeduplicatingCloudRunJobs(
        jobs, FirestoreJobClaims(os.environ["GOOGLE_CLOUD_PROJECT"], jobs.session),
    )
    server = FastMCP("ScribeFlow", auth=auth)

    @server.tool(
        description=(
            "Transcribe one Google Drive audio by file ID; incurs OpenAI API charges. "
            "Optional keywords are literal names or terms that may be spoken; "
            "prompt is brief factual context, never invented dialogue. "
            "Existing transcripts are not overwritten. Repeated requests for "
            "the same file reuse the existing job. Explicit retry_failed=True "
            "is required after failure or an unconfirmed stale submission."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    def transcribe_audio(
        file_id: str, keywords: list[str] | None = None, prompt: str = "",
        retry_failed: bool = False,
    ) -> dict:
        _require_owner(owner_email)
        return backend.start(
            file_id, keywords=keywords, prompt=prompt,
            retry_failed=retry_failed,
        )

    @server.tool(
        description="Check a submitted transcription without restarting it.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_transcription_status(operation: str) -> dict:
        _require_owner(owner_email)
        return backend.status(operation)

    return server


def main() -> None:
    # Tokens and Drive credentials must not be written to logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    create_server().run(
        transport="http", host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")), stateless_http=True,
    )


if __name__ == "__main__":
    main()
