"""Google-account OAuth protection for the remote ScribeFlow MCP service."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Literal

from .cloud_dedup import DeduplicatingCloudRunJobs, FirestoreJobClaims
from .cloud_run import _FILE_ID, CloudRunJobs, CloudRunTarget
from .engine import SUPPORTED_AUDIO_SUFFIXES
from .linked_drive import (
    DRIVE_SCOPE,
    MIN_TOKEN_SECONDS,
    DriveCredential,
    check_file,
    list_shared_drives,
    search_files,
)

_GOOGLE_PROFILE = "https://www.googleapis.com/auth/userinfo.profile"
_GOOGLE_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "name": {"type": "string"},
        "email": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": False,
}


def _require_owner(allowed_email: str | set[str]):
    """Prevent other Google accounts from triggering paid or Drive operations."""
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    claims = token.claims if token else {}
    emails = {allowed_email.casefold()} if isinstance(allowed_email, str) else allowed_email
    verified = claims.get("email_verified")
    if (
        not token
        or (claims.get("email") or "").casefold() not in emails
        or verified not in (True, "true", "True", "1")
        or not claims.get("sub")
    ):
        raise PermissionError("This Google account is not authorized for ScribeFlow")
    return token


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
    emails = {owner_email.casefold()}
    additional = os.environ.get("SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS", "")
    emails.update(email.casefold() for email in additional.split())
    if not os.environ.get("SCRIBEFLOW_DRIVE_JOB_KEY"):
        raise ValueError("Google Drive jobs require an encryption key")
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
    required_scopes = ["openid", _GOOGLE_EMAIL, DRIVE_SCOPE]
    valid_scopes = [*required_scopes, _GOOGLE_PROFILE]
    auth = GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=origin,
        required_scopes=required_scopes,
        valid_scopes=valid_scopes,
        extra_authorize_params={"scope": " ".join(valid_scopes), "prompt": "consent select_account"},
        token_expiry_threshold_seconds=MIN_TOKEN_SECONDS,
        jwt_signing_key=jwt_key,
        client_storage=store,
        require_authorization_consent=True,
    )
    jobs = CloudRunJobs(
        CloudRunTarget(
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            region=os.environ.get("SCRIBEFLOW_REGION", "asia-northeast3"),
            job=os.environ.get("SCRIBEFLOW_JOB", "scribeflow-transcribe"),
        ),
        previous_region=os.environ.get("SCRIBEFLOW_PREVIOUS_WORKER_REGION"),
    )
    backend = DeduplicatingCloudRunJobs(
        jobs, FirestoreJobClaims(os.environ["GOOGLE_CLOUD_PROJECT"], jobs.session),
    )
    server = FastMCP("ScribeFlow", auth=auth)

    @server.tool(
        meta={"openai/profile": True},
        output_schema=_PROFILE_SCHEMA,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def get_profile() -> dict:
        """Identify the Google account authenticated for this connection."""
        claims = _require_owner(emails).claims
        profile = {"id": str(claims["sub"]), "email": claims["email"]}
        if claims.get("name"):
            profile["name"] = claims["name"]
        return profile

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def get_google_profile_photo() -> dict:
        """Get the Google profile image URL for the current connection, when available."""
        return {"picture": _require_owner(emails).claims.get("picture")}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_drive_files(
        name: str = "", page_token: str = "", drive_id: str = "", include_shared_drives: bool = True,
    ) -> dict:
        """List Drive files by name prefix or list all files. Use nextPageToken to continue.

        If incompleteSearch is true, list individual shared drives or set
        include_shared_drives=false for the personal and directly shared file view.
        """
        return search_files(
            DriveCredential.from_connection(_require_owner(emails)), name, page_token, drive_id,
            include_shared_drives=include_shared_drives,
        )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_google_shared_drives(page_token: str = "") -> dict:
        """List the shared drives this account can browse individually if global search is incomplete."""
        return list_shared_drives(DriveCredential.from_connection(_require_owner(emails)), page_token)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def check_drive_file(file_id: str) -> dict:
        """Check a file's download and output folder permissions without transcribing."""
        return check_file(DriveCredential.from_connection(_require_owner(emails)), file_id)

    def start(file_id, keywords, prompt, retry, language, *, replace_existing=False, transcript_file_id="", output_format="txt"):
        token = _require_owner(emails)
        options = {}
        if transcript_file_id:
            if not replace_existing or not _FILE_ID.fullmatch(transcript_file_id):
                raise ValueError("An existing Drive transcript ID is required for explicit replacement")
            options["transcript_file_id"] = transcript_file_id
        if not _FILE_ID.fullmatch(file_id):
            raise ValueError("A valid Google Drive file ID is required")
        credential = DriveCredential.from_connection(token)
        file = check_file(credential, file_id)
        if not file["can_download"]:
            raise PermissionError("The connected account must be able to read the audio")
        if os.path.splitext(file["name"])[1].lower() not in SUPPORTED_AUDIO_SUFFIXES:
            raise ValueError("Select a supported audio file")
        options.update(owner=str(token.claims["sub"]), drive_envelope=credential.seal(file_id))
        return backend.start(
            file_id, keywords=keywords, prompt=prompt, retry=retry, language=language,
            **({"output_format": output_format} if output_format != "txt" else {}),
            **({"replace_existing": True} if replace_existing else {}), **options,
        )

    @server.tool(
        description=(
            "Transcribe one Google Drive audio by file ID; incurs OpenAI API charges. "
            "Optional keywords are literal names or terms that may be spoken; "
            "prompt is brief factual context, never invented dialogue. "
            "language defaults to automatic detection ('auto'); use 'ko', 'en', "
            "or another supported code to hint a known language. "
            "output_format selects one output file: 'txt' (default, plain text) or 'md' (Markdown). "
            "Matching adjacent TXT or Markdown transcripts are reused without another OpenAI request unless "
            "their recorded OpenAI model has a supported upgrade to the configured model. "
            "A supported upgrade re-transcribes and updates the existing file in its original format and location, "
            "preserving its ID; detected manual edits, unknown models and source mismatches are not overwritten. "
            "Legacy transcripts without a body checksum cannot be checked for prior manual edits. "
            "Concurrent requests for the same audio reuse the active job; later requests recheck current Drive/model state. "
            "Explicit retry=True is required to repeat a failed job or stale submission. "
            "Do not retranscribe solely to convert an existing transcript to another format."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    def transcribe_audio(
        file_id: str, keywords: list[str] | None = None, prompt: str = "",
        retry: bool = False, language: str = "auto", output_format: Literal["txt", "md"] = "txt",
    ) -> dict:
        return start(file_id, keywords, prompt, retry, language, output_format=output_format)

    @server.tool(
        description=(
            "Re-transcribe one explicitly selected Drive recording with the configured OpenAI model "
            "and replace the existing ScribeFlow transcript in place. This incurs OpenAI charges. "
            "Only use when the user explicitly requests replacing that transcription. "
            "output_format must match the existing file: 'txt' (default, plain text) or 'md' (Markdown). "
            "Requires an existing transcript whose recorded source SHA-256 matches the audio. "
            "Set transcript_file_id to select the existing transcript explicitly, including after it has moved to another Drive folder; "
            "the existing file is updated at its current ID and location without changing its format. "
            "Use get_transcription_status to follow the returned operation."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    def retranscribe_audio(
        file_id: str, keywords: list[str] | None = None, prompt: str = "",
        language: str = "auto", transcript_file_id: str = "", output_format: Literal["txt", "md"] = "txt",
    ) -> dict:
        return start(
            file_id, keywords, prompt, True, language, replace_existing=True,
            transcript_file_id=transcript_file_id, output_format=output_format,
        )

    @server.tool(
        description="Check a submitted transcription without restarting it. Terminal results include the actual output format, Drive file ID and URL when available, or a safe failure code.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_transcription_status(operation: str) -> dict:
        token = _require_owner(emails)
        return backend.status(operation, owner=str(token.claims["sub"]))

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
