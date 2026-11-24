"""Use the Google Drive credentials of the account selected by the MCP client."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
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
        "id": file["id"], "name": file["name"], "parent_id": parents[0] if len(parents) == 1 else None,
        "drive_id": file.get("driveId"),
        "can_download": bool(file.get("capabilities", {}).get("canDownload")),
        "can_save_transcript": (
            parent.get("mimeType") == _FOLDER and not parent.get("trashed")
            and bool(parent.get("capabilities", {}).get("canAddChildren"))
        ),
    }


def can_add_to_folder(credential: DriveCredential, parent_id: str) -> bool:
    parent = _metadata(credential, parent_id, "mimeType,trashed,capabilities(canAddChildren)")
    return (parent.get("mimeType") == _FOLDER and not parent.get("trashed")
            and bool(parent.get("capabilities", {}).get("canAddChildren")))


FILE_FIELDS = "id,name,mimeType,parents,driveId,trashed,size,sha256Checksum,version,capabilities(canDownload,canEdit,canModifyContent)"


def file_metadata(credential: DriveCredential, file_id: str) -> dict:
    return _metadata(credential, file_id, FILE_FIELDS)


def sibling_transcripts(credential: DriveCredential, source: dict, names: tuple[str, ...]) -> list[dict]:
    """List only named siblings, including duplicates, using the actual parent ID."""
    parents = source.get("parents") or []
    if len(parents) != 1:
        return []
    query = "trashed = false and " + _quote_query(parents[0]) + " in parents and (" + " or ".join(
        "name = " + _quote_query(name) for name in names
    ) + ")"
    params = {"q": query, "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
              "pageSize": 100, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
              "corpora": "user"}
    if source.get("driveId"):
        params.update(corpora="drive", driveId=source["driveId"])
    entries = []
    while True:
        response = _read(credential, "files", **params)
        if response.get("incompleteSearch"):
            raise RuntimeError("Drive sibling search is incomplete")
        entries.extend(response.get("files", []))
        if not response.get("nextPageToken"):
            return entries
        params["pageToken"] = response["nextPageToken"]


def download_file(credential: DriveCredential, file_id: str, destination: Path) -> None:
    """Stream the selected binary by ID; never resolve it through its name."""
    if not _DRIVE_ID.fullmatch(file_id):
        raise ValueError("Invalid Drive file ID")
    request = Request(_API + "files/" + file_id + "?alt=media&supportsAllDrives=true",
                      headers={"Authorization": "Bearer " + credential.access_token})
    with urlopen(request, timeout=60) as reply, destination.open("wb") as output:
        shutil.copyfileobj(reply, output, length=1024 * 1024)


def create_transcript(credential: DriveCredential, parent_id: str, name: str, data: bytes, mime_type: str) -> dict:
    """Publish the small final text with metadata and get the actual new file ID."""
    boundary = uuid.uuid4().hex
    metadata = json.dumps({"name": name, "parents": [parent_id], "mimeType": mime_type}, ensure_ascii=False).encode()
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + metadata
            + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode() + data
            + f"\r\n--{boundary}--\r\n".encode())
    request = Request("https://www.googleapis.com/upload/drive/v3/files?" + urlencode({
        "uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,sha256Checksum",
    }), headers={"Authorization": "Bearer " + credential.access_token,
                 "Content-Type": f"multipart/related; boundary={boundary}"}, method="POST", data=body)
    with urlopen(request, timeout=60) as reply:
        return json.load(reply)
