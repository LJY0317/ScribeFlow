"""Google-account OAuth protection for the remote ScribeFlow MCP service."""

from __future__ import annotations

import base64
import hashlib
import logging
import os

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
    linked = os.environ.get("SCRIBEFLOW_LINKED_DRIVE") == "1"
    emails = {owner_email.casefold()}
    if linked:
        additional = os.environ.get("SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS", "")
        emails.update(email.casefold() for email in additional.split())
        if not os.environ.get("SCRIBEFLOW_DRIVE_JOB_KEY"):
            raise ValueError("Linked Google Drive requires a job encryption key")
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
    required_scopes = ["openid", _GOOGLE_EMAIL]
    if linked:
        required_scopes.append(DRIVE_SCOPE)
    valid_scopes = [*required_scopes, _GOOGLE_PROFILE]
    auth = GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=origin,
        required_scopes=required_scopes,
        valid_scopes=valid_scopes,
        extra_authorize_params={"scope": " ".join(valid_scopes), "prompt": "consent select_account"},
        token_expiry_threshold_seconds=MIN_TOKEN_SECONDS if linked else 0,
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

    def linked_credential(token) -> DriveCredential:
        if not linked:
            raise PermissionError("This deployment has not enabled per-account Google Drive access")
        return DriveCredential.from_connection(token)

    if linked:
        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def search_drive_files(
            name: str = "", page_token: str = "", drive_id: str = "", include_shared_drives: bool = True,
        ) -> dict:
            """List Drive files by name prefix or list all files. Use nextPageToken to continue.

            If incompleteSearch is true, list individual shared drives or set
            include_shared_drives=false for the personal and directly shared file view.
            """
            return search_files(
                linked_credential(_require_owner(emails)), name, page_token, drive_id,
                include_shared_drives=include_shared_drives,
            )

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def list_google_shared_drives(page_token: str = "") -> dict:
            """List the shared drives this account can browse individually if global search is incomplete."""
            return list_shared_drives(linked_credential(_require_owner(emails)), page_token)

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def check_drive_file(file_id: str) -> dict:
            """Check a file's download and output folder permissions without transcribing."""
            return check_file(linked_credential(_require_owner(emails)), file_id)

    def start(file_id, keywords, prompt, retry, language, *, replace_existing=False, transcript_file_id="", output_format="txt"):
        token = _require_owner(emails)
        options = {}
        if transcript_file_id:
            if not replace_existing or not _FILE_ID.fullmatch(transcript_file_id):
                raise ValueError("An existing Drive TXT ID is required for explicit replacement")
            options["transcript_file_id"] = transcript_file_id
        if linked:
            if not _FILE_ID.fullmatch(file_id):
                raise ValueError("A valid Google Drive file ID is required")
            credential = linked_credential(token)
            file = check_file(credential, file_id)
            if not file["can_download"] or (not replace_existing and not file["can_save_transcript"]):
                raise PermissionError("The connected account must be able to read the audio and save its TXT")
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
            "Existing transcripts are not overwritten. Repeated requests for "
            "the same file reuse the existing job. Explicit retry=True "
            "is required to repeat a completed/failed job or stale submission; "
            "retrying can incur new API charges."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    def transcribe_audio(
        file_id: str, keywords: list[str] | None = None, prompt: str = "",
        retry: bool = False, language: str = "auto", output_format: str = "txt",
    ) -> dict:
        return start(file_id, keywords, prompt, retry, language, output_format=output_format)

    @server.tool(
        description=(
            "Re-transcribe one explicitly selected Drive recording with the configured OpenAI model "
            "and replace the existing ScribeFlow TXT in place. This incurs OpenAI charges. "
            "Only use when the user explicitly requests replacing that transcription. "
            "Requires an existing TXT whose recorded source SHA-256 matches the audio. "
            "Set transcript_file_id when the original TXT is in a different Drive folder; "
            "the existing TXT is updated at its current ID and location. "
            "Use get_transcription_status to follow the returned operation."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    def retranscribe_audio(
        file_id: str, keywords: list[str] | None = None, prompt: str = "",
        language: str = "auto", transcript_file_id: str = "", output_format: str = "txt",
    ) -> dict:
        return start(
            file_id, keywords, prompt, True, language, replace_existing=True,
            transcript_file_id=transcript_file_id, output_format=output_format,
        )

    @server.tool(
        description="Check a submitted transcription without restarting it.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_transcription_status(operation: str) -> dict:
        token = _require_owner(emails)
        return backend.status(operation, **({"owner": str(token.claims["sub"])} if linked else {}))

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
