"""EnolaAdapter — Tier 2 Go username search (Section 11.8)."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time

from worker_python.adapters.base import ToolAdapter
from worker_go.adapters import go_binary
from api.models.evidence import EvidenceUnit


# enola scans every site concurrently and streams results into its TUI list as
# each responds. Quit the TUI once output has been quiet for QUIET_SECONDS (the
# scan has drained) but never before MIN_RUN_SECONDS nor after MAX_RUN_SECONDS.
MIN_RUN_SECONDS = 12.0
QUIET_SECONDS = 10.0
MAX_RUN_SECONDS = 180.0


class EnolaAdapter(ToolAdapter):
    """Wraps the enola Go binary.

    enola is a Bubble Tea TUI: it requires a real terminal (PTY) and only writes
    its ``-o {file}.json`` results — a JSON array of ``{title, url, found}`` — in
    a deferred handler *after* the TUI exits (on ``q`` / Ctrl-C). There is no
    headless/JSON-on-stdout mode, so we drive it under a pseudo-terminal: stream
    its output, send ``q`` once the scan goes quiet, then read the output file.
    """

    def name(self) -> str:
        return "enola"

    def version(self) -> str:
        return "go"

    def get_tool_tier(self) -> int:
        return 2

    def health_check(self) -> bool:
        path = go_binary("enola")
        return os.path.exists(path) and os.access(path, os.X_OK)

    def _run_under_pty(self, cmd: list[str]) -> None:
        """Run enola attached to a PTY and quit it once the scan settles."""
        master_fd, slave_fd = os.openpty()
        try:
            # Give the TUI a sane terminal size so its list renders/collects.
            try:
                winsize = struct.pack("HHHH", 50, 200, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")

            # Bubble Tea opens /dev/tty directly, so the PTY slave must be the
            # child's *controlling terminal* — not merely its stdio. login_tty()
            # makes the child a session leader and adopts the slave as its
            # controlling tty (and stdin/stdout/stderr).
            captured_slave = slave_fd

            def _preexec() -> None:
                try:
                    os.login_tty(captured_slave)
                except (AttributeError, OSError):
                    os.setsid()
                    fcntl.ioctl(captured_slave, termios.TIOCSCTTY, 0)

            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                close_fds=True,
                preexec_fn=_preexec,
            )
            os.close(slave_fd)
            slave_fd = -1

            started = time.monotonic()
            last_output = started
            quit_sent = False

            while True:
                if proc.poll() is not None:
                    break

                now = time.monotonic()
                elapsed = now - started
                idle = now - last_output
                if not quit_sent and (
                    elapsed >= MAX_RUN_SECONDS
                    or (elapsed >= MIN_RUN_SECONDS and idle >= QUIET_SECONDS)
                ):
                    # 'q' quits the Bubble Tea list; Ctrl-C is the fallback. The
                    # deferred JSON export runs as the program exits.
                    try:
                        os.write(master_fd, b"q")
                        time.sleep(0.3)
                        os.write(master_fd, b"\x03")
                    except OSError:
                        pass
                    quit_sent = True

                try:
                    readable, _, _ = select.select([master_fd], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if readable:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError as exc:
                        if exc.errno == errno.EIO:  # PTY closed on child exit
                            break
                        chunk = b""
                    if chunk:
                        last_output = time.monotonic()
                    else:
                        break

                # Hard ceiling guard if quit was sent but the process lingers.
                if quit_sent and (time.monotonic() - started) >= MAX_RUN_SECONDS + 15:
                    break

            self._reap(proc)
        finally:
            for fd in (master_fd, slave_fd):
                if fd is not None and fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        """Ensure the enola process is gone, escalating if necessary."""
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                return
            try:
                proc.send_signal(sig)
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                continue

    def run(self, seed: str) -> list[dict]:
        binary = go_binary("enola")
        fd, out_file = tempfile.mkstemp(prefix="enola_", suffix=".json")
        os.close(fd)
        os.unlink(out_file)  # let enola create it fresh (json by extension)
        try:
            self._run_under_pty([binary, seed, "-o", out_file])
            try:
                with open(out_file, "r", encoding="utf-8", errors="ignore") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                return []
        finally:
            try:
                os.unlink(out_file)
            except OSError:
                pass

        return data if isinstance(data, list) else []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            # enola writes every probed site; keep only confirmed hits.
            if not item.get("found", False):
                continue
            url = item.get("url") or item.get("link") or ""
            if not url:
                continue
            platform = item.get("title") or item.get("name") or item.get("site") or "unknown"
            units.append(
                self.make_evidence(
                    source_platform=str(platform).lower(),
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=url,
                )
            )
        return units
