"""Best-effort, read-only Drive ID lookup using an existing rclone OAuth token.

Only the Cloud Run worker's private RCLONE_CONFIG copy is inspected. Credentials
are never logged or persisted by this module. The caller must verify that the
resulting rclone path resolves back to the requested file ID before using it.

A missing/expired token, inaccessible parent, or unsupported rclone config is
not fatal: the caller can fall back to the existing rclone directory walk.
"""

from __future__ import annotations

import configparser
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_ID = re.compile(r"[A-Za-z0-9_-]{10,}")
_DRIVE_API = "https://www.googleapis.com/drive/v3/files/"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_MAX_DEPTH = 32


@dataclass(frozen=True)
class DrivePathCandidate:
    relative_path: str
    shared_with_me: bool


def _existing_access_token() -> str | None:
    """Read only the token for an explicitly configured, local rclone remote.

    We deliberately avoid printing config dumps or taking ownership of OAuth
    refresh tokens. The caller can ask rclone to refresh its own token first.
    """
    config_path = os.environ.get("RCLONE_CONFIG")
    if not config_path:
        return None
    try:
        config = configparser.ConfigParser(interpolation=None)
        with Path(config_path).open("r", encoding="utf-8") as stream:
            config.read_file(stream)
        if config.get("gdrive", "type") != "drive":
            return None
        token_info = json.loads(config.get("gdrive", "token"))
        token = token_info.get("access_token")
        expiry_raw = token_info.get("expiry")
        if not token or not expiry_raw:
            return None
        expiry = datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            return None
        if expiry.astimezone(timezone.utc) <= (
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ):
            return None
        return token
    except (OSError, ValueError, TypeError, KeyError, configparser.Error):
        return None


def _drive_metadata(file_id: str, token: str) -> dict:
    if not _ID.fullmatch(file_id):
        raise ValueError("Invalid Google Drive metadata ID")
    query = urlencode({
        "fields": "id,name,mimeType,parents,trashed",
        "supportsAllDrives": "true",
    })
    url = _DRIVE_API + file_id + "?" + query
    # Do not log request headers: Authorization contains the rclone token.
    request = Request(
        url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}
    )
    with urlopen(request, timeout=12) as reply:
        return json.loads(reply.read())


def _valid_component(name: object) -> bool:
    return (
        isinstance(name, str) and len(name) > 0
        and name not in {".", ".."}
        and not any(ch in name for ch in "/\\\r\n\0")
    )


def candidate_from_id(file_id: str, root_folder_id: str | None = None) -> DrivePathCandidate | None:
    """Resolve by ID in O(folder depth), never by listing the entire Drive.

    The caller is responsible for confirming this path with rclone --stat.
    For a folder shared from another account, we stop at the last accessible
    ancestor and propose the rclone shared-with-me view.
    """
    token = _existing_access_token()
    if not token or not _ID.fullmatch(file_id):
        return None
    if root_folder_id and not _ID.fullmatch(root_folder_id):
        return None
    try:
        root_id = root_folder_id or str(_drive_metadata("root", token)["id"])
        names: list[str] = []
        seen = set()
        current_id = file_id
        for depth in range(_MAX_DEPTH):
            if current_id == root_id:
                if not names:
                    return None
                return DrivePathCandidate("/".join(reversed(names)), False)
            if current_id in seen:
                return None
            seen.add(current_id)
            try:
                info = _drive_metadata(current_id, token)
            except HTTPError as exc:
                # Inaccessible parent of a shared-with-me folder/file. The
                # authenticated user's My Drive root is never crossed.
                if exc.code in (403, 404) and names and not root_folder_id:
                    return DrivePathCandidate("/".join(reversed(names)), True)
                return None
            if info.get("trashed") or info.get("id") != current_id:
                return None
            name = info.get("name")
            if not _valid_component(name):
                return None
            names.append(name)
            parents = info.get("parents", [])
            if not parents:
                if root_folder_id:
                    return None
                return DrivePathCandidate("/".join(reversed(names)), True)
            if len(parents) != 1 or not _ID.fullmatch(str(parents[0])):
                return None
            current_id = str(parents[0])
    except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError):
        return None
    return None
