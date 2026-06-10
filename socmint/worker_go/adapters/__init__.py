"""Go-binary tool adapters."""
import os

# Directory holding compiled Go binaries (mounted into the worker_go container).
GO_TOOLS_DIR = os.environ.get("GO_TOOLS_DIR", "/app/tools/go")


def go_binary(name: str) -> str:
    """Absolute path to a compiled Go binary by name."""
    return os.path.join(GO_TOOLS_DIR, name)
