"""One image, two Cloud Run runtimes selected by environment."""

import os


def main() -> int:
    mode = os.environ.get("SCRIBEFLOW_CLOUD_MODE", "worker")
    if mode == "worker":
        from .cloud_worker import main as worker
        return worker()
    if mode == "gateway":
        from .cloud_gateway import main as server
        server()
        return 0
    raise ValueError("Unknown ScribeFlow Cloud Run mode")


if __name__ == "__main__":
    raise SystemExit(main())
