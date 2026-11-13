"""Small Cloud Run Jobs API client; no database or polling server needed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_FILE_ID = re.compile(r"[A-Za-z0-9_-]{20,}")
_GOOGLE_ID = re.compile(r"[a-z][a-z0-9-]{2,62}")
_REGION = re.compile(r"[a-z]+-[a-z]+[0-9]+")


@dataclass(frozen=True)
class CloudRunTarget:
    project: str
    region: str
    job: str

    def __post_init__(self) -> None:
        if not _GOOGLE_ID.fullmatch(self.project):
            raise ValueError("Invalid Google Cloud project ID")
        if not _REGION.fullmatch(self.region):
            raise ValueError("Invalid Google Cloud region")
        if not _GOOGLE_ID.fullmatch(self.job):
            raise ValueError("Invalid Cloud Run job name")

    @property
    def resource(self) -> str:
        return f"projects/{self.project}/locations/{self.region}/jobs/{self.job}"

    @property
    def operation_prefix(self) -> str:
        return f"projects/{self.project}/locations/{self.region}/operations/"


class CloudRunJobs:
    def __init__(self, target: CloudRunTarget, session: Any = None):
        self.target = target
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            session = AuthorizedSession(credentials)
        self.session = session

    def _call(self, verb: str, resource: str, *, payload: dict | None = None) -> dict:
        url = "https://run.googleapis.com/v2/" + resource
        response = self.session.request(verb, url, json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def start(self, file_id: str) -> dict:
        """Queue exactly one job without downloading private Drive content."""
        if not _FILE_ID.fullmatch(file_id):
            raise ValueError("A valid Google Drive file ID is required")
        variables = [{"name": "SCRIBEFLOW_SOURCE_FILE_ID", "value": file_id}]
        operation = self._call("POST", self.target.resource + ":run", payload={
            "overrides": {
                "containerOverrides": [{
                    "env": variables,
                }],
                "taskCount": 1,
            }
        })
        name = operation.get("name", "")
        if not name.startswith(self.target.operation_prefix):
            raise RuntimeError("Unexpected Cloud Run operation name")
        return {"status": "submitted", "operation": name}

    def status(self, operation_name: str) -> dict:
        """Inspect an existing execution; do not resubmit or incur API costs."""
        if not (
            operation_name.startswith(self.target.operation_prefix)
            and re.fullmatch(r"[A-Za-z0-9_-]+",
                             operation_name[len(self.target.operation_prefix):])
        ):
            raise ValueError("Operation is not in the configured Cloud Run project")
        operation = self._call("GET", operation_name)
        if not operation.get("done"):
            return {"status": "running", "operation": operation_name}
        if operation.get("error"):
            return {"status": "failed", "operation": operation_name}
        execution_name = operation.get("response", {}).get("name", "")
        if not execution_name.startswith(self.target.resource + "/executions/"):
            raise RuntimeError("Unexpected Cloud Run execution name")
        execution = self._call("GET", execution_name)
        if execution.get("failedCount") or execution.get("cancelledCount"):
            state = "failed"
        elif execution.get("completionTime") and execution.get("succeededCount", 0) >= 1:
            state = "completed"
        else:
            state = "running"
        result = {"status": state, "operation": operation_name}
        # Console logs require the user's normal Google Cloud permissions.
        if execution.get("logUri"):
            result["logs_url"] = execution["logUri"]
        return result
