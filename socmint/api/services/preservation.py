"""MODULE 4 — Preservation Service.

Preserves every positive hit BEFORE it is written to the database: fetch HTML,
store to MinIO, hash it, screenshot via GoWitness, submit a Wayback save, and
pull a prior archived snapshot if one exists. Never drops evidence on failure.
See MODULE 4 (Section 5) of SOCMINT_PLAN_v2_0.txt.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx

from api.db import minio_client

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class PreservationService:
    """Screenshot + hash + archive every positive hit."""

    def preserve(self, url: str, evidence_id: UUID, case_id: UUID) -> dict:
        """Run the 7-step preservation sequence; return preservation refs."""
        base_path = f"cases/{case_id}/{evidence_id}"
        snapshot_ref: Optional[str] = None
        snapshot_hash: Optional[str] = None
        wayback_ref: Optional[str] = None

        # Steps 1-3: fetch HTML, store to MinIO, hash.
        try:
            html_bytes = self._fetch_html(url)
            snapshot_ref = f"{base_path}/raw.html"
            minio_client.put_bytes(snapshot_ref, html_bytes, content_type="text/html")
            snapshot_hash = hashlib.sha256(html_bytes).hexdigest()
        except Exception as exc:  # noqa: BLE001
            logger.warning("preservation html step failed for %s: %s", url, exc)

        # Step 4-5: screenshot via GoWitness, upload to MinIO.
        try:
            self._screenshot(url, base_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preservation screenshot step failed for %s: %s", url, exc)

        # Step 6: submit Wayback save request.
        try:
            wayback_ref = self._wayback_save(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preservation wayback step failed for %s: %s", url, exc)

        # Step 7: pull prior archived snapshot if one exists.
        try:
            self._pull_prior_snapshot(url, base_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("preservation snapshot pull failed for %s: %s", url, exc)

        # Step 8: check for an existing archive.today snapshot (best-effort).
        archive_today_ref: Optional[str] = None
        try:
            archive_today_ref = self._archive_today_check(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive.today check failed for %s: %s", url, exc)

        return {
            "snapshot_ref": snapshot_ref,
            "snapshot_hash": snapshot_hash,
            "wayback_ref": wayback_ref,
            "archive_today_ref": archive_today_ref,
            "preserved_at": datetime.now(timezone.utc).isoformat(),
        }

    # ---- steps --------------------------------------------------------------
    def _fetch_html(self, url: str) -> bytes:
        with httpx.Client(timeout=10.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content

    def _screenshot(self, url: str, base_path: str) -> None:
        from worker_go.adapters.gowitness import GoWitnessAdapter

        adapter = GoWitnessAdapter()
        if not adapter.health_check():
            logger.info("gowitness unavailable; skipping screenshot for %s", url)
            return
        local_png = "/tmp/socmint_shot.png"
        result = adapter.run(url, output_path=local_png)
        captured = result and result[0].get("captured")
        if captured:
            with open(result[0]["screenshot_path"], "rb") as handle:
                minio_client.put_bytes(
                    f"{base_path}/screenshot.png", handle.read(), content_type="image/png"
                )

    def _wayback_save(self, url: str) -> Optional[str]:
        save_url = f"https://web.archive.org/save/{url}"
        with httpx.Client(timeout=6.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(save_url)
            if response.status_code == 429:
                logger.warning("wayback rate-limited (429) for %s", url)
                return None
            content_location = response.headers.get("Content-Location")
            if content_location:
                return f"https://web.archive.org{content_location}"
            return f"https://web.archive.org/web/{url}"

    def _pull_prior_snapshot(self, url: str, base_path: str) -> None:
        # Resolve the closest archived snapshot via the Wayback availability API
        # (one bounded request) instead of the multi-minute waybackpy CDX crawl,
        # which dominated task time whenever archive.org was slow or unreachable.
        try:
            with httpx.Client(timeout=6.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
                avail = client.get(
                    "https://archive.org/wayback/available", params={"url": url}
                )
                closest = avail.json().get("archived_snapshots", {}).get("closest", {})
                latest = closest.get("url") if closest.get("available") else None
                if not latest:
                    return
                response = client.get(latest)
                if response.status_code == 200:
                    minio_client.put_bytes(
                        f"{base_path}/wayback_snapshot.html",
                        response.content,
                        content_type="text/html",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("wayback snapshot pull failed: %s", exc)

    def _archive_today_check(self, url: str) -> Optional[str]:
        """Best-effort check for an existing archive.today snapshot.

        Only checks for *existing* snapshots (GET with redirect follow).
        Never triggers new captures (no CAPTCHA risk, no write operations).
        Returns the snapshot URL or None — never raises.
        """
        try:
            check_url = f"https://archive.ph/newest/{url}"
            with httpx.Client(timeout=8.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
                response = client.get(check_url)
                # archive.ph redirects to the snapshot if one exists
                if response.status_code == 200 and "archive.ph" in str(response.url):
                    final = str(response.url)
                    if final != check_url and "/newest/" not in final:
                        return final
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("archive.today check failed: %s", exc)
            return None
