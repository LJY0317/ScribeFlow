"""Use the Google Drive credentials of the account selected by the MCP client."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
JOB_TIMEOUT_SECONDS = 2700
MIN_TOKEN_SECONDS = JOB_TIMEOUT_SECONDS + 300
_API = "https://www.googleapis.com/drive/v3/"
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,}")
_FOLDER = "application/vnd.google-apps.folder"


@dataclass(frozen=True, repr=False)
class DriveCredential:
    access_token: str
    expires_at: int

    @classmethod
    def from_connection(cls, token):
        if DRIVE_SCOPE not in token.scopes:
            raise PermissionError("Reconnect this Google account and allow Google Drive access")
        return cls(token.token, int(token.expires_at or 0))

    def seal(self, file_id: str) -> str:
        from cryptography.fernet import Fernet

        if self.expires_at - time.time() < MIN_TOKEN_SECONDS:
            raise PermissionError("Google Drive authorization is too close to expiry; reconnect and retry")
        payload = json.dumps([file_id, self.access_token, self.expires_at]).encode()
        return Fernet(os.environ["SCRIBEFLOW_DRIVE_JOB_KEY"]).encrypt(payload).decode()

    @classmethod
    def open(cls, envelope: str, file_id: str):
        from cryptography.fernet import Fernet

        payload = Fernet(os.environ["SCRIBEFLOW_DRIVE_JOB_KEY"]).decrypt(envelope)
        bound_file, token, expires_at = json.loads(payload)
        if bound_file != file_id or expires_at - time.time() < JOB_TIMEOUT_SECONDS + 60:
            raise PermissionError("The job's Google Drive authorization is invalid or expires too soon")
        return cls(token, expires_at)

    def rclone_config(self, drive_id: str | None) -> str:
        expiry = datetime.fromtimestamp(self.expires_at, timezone.utc).isoformat()
        token = json.dumps({"access_token": self.access_token, "token_type": "Bearer", "expiry": expiry})
        config = f"[gdrive]\ntype = drive\nscope = drive\ntoken = {token}\n"
        if drive_id:
            if not _DRIVE_ID.fullmatch(drive_id):
                raise ValueError("Invalid shared drive ID")
            config += f"team_drive = {drive_id}\n"
        return config


def _read(credential: DriveCredential, resource: str, **params) -> dict:
    url = _API + resource + "?" + urlencode(params)
    request = Request(url, headers={"Authorization": "Bearer " + credential.access_token})
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise PermissionError("This Google connection cannot access the requested Drive resource") from None
        raise


def _quote_query(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def search_files(
    credential: DriveCredential, name: str = "", page_token: str = "", drive_id: str = "",
    *, include_shared_drives: bool = True,
) -> dict:
    """Search names in My Drive, shared files, and shared drives, one page at a time."""
    if drive_id and not _DRIVE_ID.fullmatch(drive_id):
        raise ValueError("Invalid shared drive ID")
    corpora = "user"
    if drive_id:
        corpora = "drive"
    elif include_shared_drives:
        corpora = "allDrives"
    query = "trashed = false"
    if name:
        query += " and name contains " + _quote_query(name)
    params = {
        "q": query,
        "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,size,webViewLink,parents,driveId)",
        "pageSize": 100,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "false" if corpora == "user" else "true",
        "corpora": corpora,
    }
    if drive_id:
        params["driveId"] = drive_id
    if page_token:
        params["pageToken"] = page_token
    return _read(credential, "files", **params)


def list_shared_drives(credential: DriveCredential, page_token: str = "") -> dict:
    params = {"fields": "nextPageToken,drives(id,name)", "pageSize": 100}
    if page_token:
        params["pageToken"] = page_token
    return _read(credential, "drives", **params)


def _metadata(credential: DriveCredential, file_id: str, fields: str) -> dict:
    if not _DRIVE_ID.fullmatch(file_id):
        raise ValueError("A valid Google Drive file ID is required")
    return _read(credential, "files/" + file_id, fields=fields, supportsAllDrives="true")


def check_file(credential: DriveCredential, file_id: str) -> dict:
    """Check download and adjacent output permission without reading audio or submitting a job."""
    file = _metadata(
        credential, file_id, "id,name,mimeType,parents,driveId,trashed,capabilities(canDownload)",
    )
    if file.get("trashed") or file.get("mimeType") == _FOLDER:
        raise ValueError("Select a non-trashed recording")
    parents = file.get("parents", [])
    parent = {}
    if len(parents) == 1:
        try:
            parent = _metadata(credential, parents[0], "id,mimeType,trashed,capabilities(canAddChildren)")
        except PermissionError:
            pass
    return {
        "id": file["id"], "name": file["name"], "parent_id": parent.get("id"),
        "drive_id": file.get("driveId"),
        "can_download": bool(file.get("capabilities", {}).get("canDownload")),
        "can_save_transcript": (
            parent.get("mimeType") == _FOLDER and not parent.get("trashed")
            and bool(parent.get("capabilities", {}).get("canAddChildren"))
        ),
    }
