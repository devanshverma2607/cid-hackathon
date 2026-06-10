"""MODULE 2 — ToolAdapter abstract base class.

Every OSINT tool is wrapped by a subclass of ToolAdapter. Tools are invoked as
subprocesses (never imported) to avoid dependency conflicts. The base class
provides graceful degradation: any failure yields a single `unavailable`
EvidenceUnit rather than raising. See MODULE 2 (Section 5) of
SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

import abc
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from api.models.evidence import EvidenceUnit

logger = logging.getLogger(__name__)


# Root directories under which git-distributed OSINT tools are cloned at image
# build time (one sub-directory per tool). Adapters invoke these tools as
# ``python <script>.py`` and rely on run_subprocess() to locate the script here
# and execute it from inside its own repository.
TOOL_ROOTS = tuple(
    p
    for p in (
        os.environ.get("PY_TOOLS_DIR", "/tools/python"),
        "/opt/tools/python",
    )
    if p
)

# Cache of resolved script paths so repeated lookups stay cheap.
_SCRIPT_CACHE: dict[str, tuple[Optional[str], str]] = {}


def resolve_tool_script(script: str) -> tuple[Optional[str], str]:
    """Locate a tool entry-script under the provisioned tool roots.

    Returns ``(script_dir, absolute_script_path)``. When the script cannot be
    found, ``script_dir`` is ``None`` and the original ``script`` is returned so
    the caller can attempt a plain invocation (which will then degrade to an
    'unavailable' evidence unit if the tool is genuinely missing).
    """
    base = os.path.basename(str(script))
    if base in _SCRIPT_CACHE:
        return _SCRIPT_CACHE[base]

    found: tuple[Optional[str], str] = (None, str(script))
    for root in TOOL_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        # Fast path: <root>/<repo>/<script> one level deep.
        try:
            for entry in os.scandir(root):
                if not entry.is_dir():
                    continue
                candidate = os.path.join(entry.path, base)
                if os.path.isfile(candidate):
                    found = (entry.path, candidate)
                    break
        except OSError:
            continue
        if found[0] is not None:
            break

    _SCRIPT_CACHE[base] = found
    return found


def tool_script_available(script: str) -> bool:
    """True when a git-distributed tool's entry-script is provisioned."""
    return resolve_tool_script(script)[0] is not None


class ToolUnavailableError(Exception):
    """Raised when a tool fails its health check or cannot run."""


class ToolAdapter(abc.ABC):
    """Abstract wrapper around a single OSINT tool."""

    # Execution context, set by execute() and consumed by make_evidence().
    _case_id: Optional[UUID] = None
    _run_id: Optional[UUID] = None
    _analyst_id: str = "system"
    _seed_type: str = "username"
    _seed_value: str = ""

    # ---- abstract interface -------------------------------------------------
    @abc.abstractmethod
    def name(self) -> str:
        """Tool name (e.g. 'sherlock')."""

    @abc.abstractmethod
    def version(self) -> str:
        """Tool version string."""

    @abc.abstractmethod
    def health_check(self) -> bool:
        """Return True if the tool is installed and runnable."""

    @abc.abstractmethod
    def run(self, seed: str) -> list[dict]:
        """Execute the tool and return raw parsed output dicts."""

    @abc.abstractmethod
    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        """Map raw output dicts into EvidenceUnit objects."""

    @abc.abstractmethod
    def get_tool_tier(self) -> int:
        """Return the tool tier (1, 2, 3, or 4)."""

    # ---- defaults -----------------------------------------------------------
    def get_proxy_tier(self) -> int:
        """1 = Tor (high sensitivity), 2 = direct egress (default)."""
        return 2

    # ---- helpers ------------------------------------------------------------
    def make_evidence(self, **fields) -> EvidenceUnit:
        """Build an EvidenceUnit, injecting the active execution context."""
        fields.setdefault("evidence_id", uuid4())
        fields.setdefault("tool_name", self.name())
        fields.setdefault("tool_version", self.version())
        fields.setdefault("tool_tier", self.get_tool_tier())
        fields.setdefault("source_tier", 2)
        fields.setdefault("seed_type", self._seed_type)
        # Backfill the seed from the active execution context when an adapter
        # leaves it blank (many adapters pass seed_value="" for hit records).
        if not str(fields.get("seed_value") or "").strip():
            fields["seed_value"] = self._seed_value
        fields["case_id"] = self._case_id or uuid4()
        fields["run_id"] = self._run_id or uuid4()
        fields["analyst_id"] = self._analyst_id
        return EvidenceUnit(**fields)

    def _make_unavailable_unit(self, seed: str, error: str) -> EvidenceUnit:
        """Build a single 'unavailable' EvidenceUnit carrying the error note."""
        return self.make_evidence(
            source_platform=self.name(),
            seed_value=seed,
            result_type="unavailable",
            result_value=seed,
            notes=error[:2000],
        )

    def run_subprocess(
        self,
        cmd: list[str],
        timeout: int = 120,
        use_tor: bool = False,
        cwd: str | None = None,
    ) -> tuple[str, str, int]:
        """Run a command, optionally via the Tor SOCKS5 proxy.

        Many git-distributed tools are invoked as ``python <script>.py ...`` and
        only resolve their own modules/data when executed from inside their
        cloned repository. When ``cwd`` is not given explicitly, we transparently
        locate such a script under the provisioned tool roots (see
        :func:`resolve_tool_script`) and run the command from that directory with
        the script rewritten to its absolute path. This lets the adapters keep
        their simple ``["python", "tool.py", ...]`` invocations unchanged.

        Returns (stdout, stderr, returncode).
        """
        env = os.environ.copy()
        if use_tor:
            tor_proxy = env.get("TOR_PROXY", "socks5://127.0.0.1:9050")
            env["ALL_PROXY"] = tor_proxy
            env["HTTP_PROXY"] = tor_proxy
            env["HTTPS_PROXY"] = tor_proxy

        run_cmd = list(cmd)
        if cwd is None and len(run_cmd) >= 2 and str(run_cmd[1]).endswith(".py"):
            script_dir, script_path = resolve_tool_script(run_cmd[1])
            if script_dir is not None:
                cwd = script_dir
                run_cmd[1] = script_path

        try:
            completed = subprocess.run(
                run_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
                cwd=cwd,
            )
            return completed.stdout or "", completed.stderr or "", completed.returncode
        except subprocess.TimeoutExpired as exc:
            return "", f"timeout after {timeout}s: {exc}", 124

    # ---- orchestration ------------------------------------------------------
    def execute(
        self,
        seed: str,
        case_id: UUID,
        run_id: UUID,
        analyst_id: str,
        seed_type: str = "username",
    ) -> list[EvidenceUnit]:
        """Health-check, run, parse, and tag results; degrade gracefully."""
        self._case_id = case_id
        self._run_id = run_id
        self._analyst_id = analyst_id
        self._seed_type = seed_type
        self._seed_value = seed

        started = time.monotonic()
        try:
            if not self.health_check():
                raise ToolUnavailableError(f"{self.name()} failed health check")

            raw = self.run(seed)
            units = self.parse(raw)

            # Attach context to every unit (defensive — make_evidence already does).
            for unit in units:
                unit.case_id = case_id
                unit.run_id = run_id
                unit.analyst_id = analyst_id
            return units
        except Exception as exc:  # noqa: BLE001 — graceful degradation is required
            logger.warning("adapter %s failed: %s", self.name(), exc)
            return [self._make_unavailable_unit(seed, str(exc))]
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info("adapter %s executed in %dms", self.name(), elapsed_ms)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
