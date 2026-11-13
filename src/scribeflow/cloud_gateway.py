"""Remote ScribeFlow MCP service protected by an unguessable endpoint path.

Anyone holding the full MCP URL can call its tools. Keep the URL private and
rotate its Secret Manager value if it is exposed.
"""

from __future__ import annotations

import os
import re

from .cloud_run import CloudRunJobs, CloudRunTarget


_ACCESS_TOKEN = re.compile(r"[A-Za-z0-9_-]{40,128}")


def create_server():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    access_token = os.environ.get("SCRIBEFLOW_MCP_ACCESS_TOKEN", "")
    if not _ACCESS_TOKEN.fullmatch(access_token):
        raise ValueError("ScribeFlow MCP access token is missing or invalid")

    backend = CloudRunJobs(CloudRunTarget(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        region=os.environ.get("SCRIBEFLOW_REGION", "asia-northeast3"),
        job=os.environ.get("SCRIBEFLOW_JOB", "scribeflow-transcribe"),
    ))
    server = FastMCP(
        "ScribeFlow", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
        streamable_http_path=f"/{access_token}/mcp",
        stateless_http=True, json_response=True,
    )

    @server.tool(
        description=(
            "Transcribe one explicitly requested Google Drive audio file. "
            "Uses paid OpenAI transcription credits; never batch-submit music."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    def transcribe_audio(file_id: str) -> dict:
        """Submit a single transcription and return the operation ID."""
        return backend.start(file_id)

    @server.tool(
        description="Check the status of one previously submitted transcription.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def get_transcription_status(operation: str) -> dict:
        """Check status without making another transcription request."""
        return backend.status(operation)

    return server


def main() -> None:
    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
