"""Discover deployed ScribeFlow resources using gcloud; never read secret payloads."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SERVICE = "scribeflow-mcp"
JOB = "scribeflow-transcribe"
REPOSITORY = "scribeflow"
PROJECT = os.environ["GCP_PROJECT"]


def query(*args):
    return json.loads(subprocess.check_output(
        ["gcloud", *args, f"--project={PROJECT}", "--format=json"],
        text=True,
    ))


def region(resource):
    return resource["metadata"]["labels"]["cloud.googleapis.com/location"]


def gateways(*args):
    return [
        service for service in query(
            "run", "services", "list", f"--filter=metadata.name={SERVICE}", *args,
        )
        if service["metadata"]["name"] == SERVICE
    ]


def gateway_route(service):
    container = service["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container.get("env", [])}
    worker_region = env.get("SCRIBEFLOW_REGION", "asia-northeast3")
    job = env.get("SCRIBEFLOW_JOB", JOB)
    return worker_region or "", job or "", container["image"]


def target():
    selected = os.environ.get("SCRIBEFLOW_REGION")
    services = gateways(*([f"--region={selected}"] if selected else []))
    if len(services) != 1:
        sys.exit(
            "Expected one existing scribeflow-mcp Gateway. For multiple regions, "
            "select SCRIBEFLOW_REGION; for a new installation, use deploy-cloud-run.sh."
        )
    service = services[0]
    worker_region, job, image = gateway_route(service)
    if not worker_region or not job:
        sys.exit("The existing Gateway Job configuration is incomplete.")
    if os.environ.get("SCRIBEFLOW_WORKER_REGION") not in (None, "", worker_region):
        sys.exit(
            f"SCRIBEFLOW_WORKER_REGION differs from the existing Gateway target "
            f"({worker_region}). Change Job routing explicitly before updating."
        )
    print(region(service), worker_region, job, image)


def inventory():
    # All regions: uninstall must find leftover jobs even after a Gateway is gone.
    routes = set()
    rows = []
    for service in gateways():
        worker_region, job, image = gateway_route(service)
        routes.add((worker_region, job))
        rows.append(("gateway", region(service), SERVICE, image,
                     service.get("status", {}).get("latestReadyRevisionName", ""),
                     worker_region, job))
    for worker in query("run", "jobs", "list"):
        name = worker["metadata"]["name"]
        location = region(worker)
        if name != JOB and (location, name) not in routes:
            continue
        spec = worker["spec"]["template"]["spec"]
        spec = spec.get("template", {}).get("spec", spec)
        container = spec["containers"][0]
        secret = next((
            item.get("valueFrom", {}).get("secretKeyRef", {}).get("name", "")
            for item in container.get("env", []) if item["name"] == "OPENAI_API_KEY"
        ), "")
        # A manually routed custom Job is removable only if it uses the
        # dedicated ScribeFlow worker account. Standard names are reserved.
        owned = name == JOB or spec.get("serviceAccountName") == (
            f"scribeflow-worker@{PROJECT}.iam.gserviceaccount.com"
        )
        rows.append(("worker" if owned else "external-worker", location,
                     name, container["image"], secret if owned else ""))
    # Standard GHCR installations need not enable Artifact Registry at all.
    # Do not trigger API activation during a read-only inventory.
    enabled = query("services", "list", "--enabled",
                    "--filter=config.name=artifactregistry.googleapis.com")
    if any(s.get("config", {}).get("name") == "artifactregistry.googleapis.com"
           for s in enabled):
        for repo in query("artifacts", "repositories", "list", "--location=all"):
            parts = repo["name"].split("/")
            if parts[-1] == REPOSITORY:
                rows.append(("registry", parts[-3], REPOSITORY))
    for row in sorted(rows):
        print("|".join(row))


# Expiry-bearing SDK collections and ScribeFlow's own operation/result records.
TTL_FIELDS = {
    "scribeflow_job_claims": "expires_at",
    "mcp-authorization-codes": "delete_at",
    "mcp-consent-csrf-tokens": "delete_at",
    "mcp-jti-mappings": "delete_at",
    "mcp-oauth-transactions": "delete_at",
    "mcp-refresh-tokens": "delete_at",
    "mcp-upstream-tokens": "delete_at",
    "mcp-rotated-refresh-tokens": "delete_at",
}


def mutate(*args):
    subprocess.run(["gcloud", *args, f"--project={PROJECT}", "--quiet"], check=True)


def retention():
    """Configure platform TTL once; no periodic application sweeper."""
    existing = query("firestore", "fields", "ttls", "list", "--database=scribeflow-oauth")
    configured = {field["name"].split("/collectionGroups/")[-1] for field in existing
                  if field.get("ttlConfig", {}).get("state") in {"ACTIVE", "CREATING"}}
    for collection, field in TTL_FIELDS.items():
        if f"{collection}/fields/{field}" not in configured:
            mutate("firestore", "fields", "ttls", "update", field,
                   f"--collection-group={collection}", "--database=scribeflow-oauth",
                   "--enable-ttl", "--async")
    print("Native Firestore TTL configured; activation/deletion is asynchronous.")


def prune(apply=False):
    """Remove only unreferenced revisions and expired completed executions."""
    services = gateways()
    if len(services) != 1:
        sys.exit("Pruning requires one verified Gateway deployment.")
    service = services[0]
    gateway_region = region(service)
    worker_region, job, image = gateway_route(service)
    worker = query("run", "jobs", "describe", job, f"--region={worker_region}")
    spec = worker["spec"]["template"]["spec"]["template"]["spec"]
    if spec["containers"][0]["image"] != image:
        sys.exit("Gateway/Worker image mismatch: no resources deleted.")
    status = service.get("status", {})
    current = status.get("latestReadyRevisionName")
    traffic = status.get("traffic", [])
    if not current or not any(t.get("revisionName") == current and t.get("percent") == 100 for t in traffic):
        sys.exit("Gateway has not completed deployment to one current revision.")
    protected = {t.get("revisionName") for t in traffic if t.get("percent", 0) or t.get("tag")}
    protected.add(current)
    executions = query("run", "jobs", "executions", "list", f"--job={job}", f"--region={worker_region}")
    if any(not e.get("status", {}).get("completionTime") for e in executions):
        sys.exit("Transcription is active; finish it before pruning release artifacts.")
    revisions = query("run", "revisions", "list", f"--service={SERVICE}", f"--region={gateway_region}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    targets = [
        ("revision", r["metadata"]["name"], gateway_region)
        for r in revisions if r["metadata"]["name"] not in protected
    ]
    targets.extend(
        ("execution", e["metadata"]["name"], worker_region)
        for e in executions if datetime.fromisoformat(
            e["status"]["completionTime"].replace("Z", "+00:00")) < cutoff
    )
    print(f"Keeping current Gateway {current} and Worker image {image}.")
    for kind, name, location in targets:
        print(f"{'Deleting' if apply else 'Would delete'} {kind} {name} ({location})", flush=True)
        if apply:
            args = ("run", "revisions", "delete") if kind == "revision" else ("run", "jobs", "executions", "delete")
            mutate(*args, name, f"--region={location}")
    print(f"{len(targets)} obsolete resources {'removed' if apply else 'identified'}.")




if __name__ == "__main__":
    if sys.argv[1:] == ["target"]:
        target()
    elif sys.argv[1:] == ["inventory"]:
        inventory()
    elif sys.argv[1:] == ["retention"]:
        retention()
    elif sys.argv[1:] in (["prune"], ["prune", "--apply"]):
        prune("--apply" in sys.argv)
    else:
        sys.exit("Usage: cloud-run-resources.py target|inventory|retention|prune [--apply]")
