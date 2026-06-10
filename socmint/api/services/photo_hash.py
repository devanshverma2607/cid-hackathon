"""MODULE 6c — Profile-photo perceptual-hash service (closes the photo pivot).

The correlation engine already scores an identical profile photo (``W_PHOTO_MATCH
= 15``, pHash Hamming distance <= 8) — but nothing in the pipeline actually
downloaded and hashed avatars, so that signal could never fire. This service
fills that gap: given an enrichment record that carries an avatar / profile-image
URL, it downloads the image, computes a perceptual hash (pHash) with Pillow +
ImageHash, and writes it back as ``enrichment['phash']`` — exactly the key the
CorrelationEngine reads.

The result is the one Maltego-style pivot the system was missing: the same photo
reused across platforms now links those accounts even when every text field
differs.
"""
from __future__ import annotations

import ipaddress
import logging
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)

# Enrichment keys (in priority order) that may hold a profile-image URL.
IMAGE_URL_KEYS = (
    "phash_source",
    "avatar_url",
    "profile_pic_url",
    "profile_pic",
    "profile_image_url",
    "profile_image",
    "avatar",
    "picture",
    "image_url",
    "image",
    "photo",
)

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB safety cap on downloaded images.
_TIMEOUT = 20.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


def _is_safe_url(url: str) -> bool:
    """Best-effort SSRF guard: only http(s) to non-private literal hosts.

    Blocks localhost, link-local and private IP literals and the cloud metadata
    host. (Public CDNs that legitimately serve avatars are unaffected.)
    """
    if not isinstance(url, str) or "://" not in url:
        return False
    scheme, _, rest = url.partition("://")
    if scheme.lower() not in ("http", "https"):
        return False
    host = rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0].strip("[]").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass  # Not an IP literal — a hostname; allowed (egress is controlled).
    return True


class PhotoHasher:
    """Download profile images and compute perceptual hashes (pHash)."""

    def hash_image_url(self, url: str) -> Optional[str]:
        """Return the hex pHash of the image at ``url`` (or None on any failure)."""
        if not _is_safe_url(url):
            return None
        try:
            import httpx

            data = b""
            with httpx.Client(
                timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
            ) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return None
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        data += chunk
                        if len(data) > MAX_IMAGE_BYTES:
                            break
            if not data:
                return None
            return self.hash_image_bytes(data)
        except Exception as exc:  # noqa: BLE001 — hashing is best-effort enrichment
            logger.debug("phash download/hash failed for %s: %s", url, exc)
            return None

    @staticmethod
    def hash_image_bytes(data: bytes) -> Optional[str]:
        """Compute the hex pHash of raw image bytes."""
        try:
            import imagehash
            from PIL import Image

            with Image.open(BytesIO(data)) as img:
                img = img.convert("RGB")
                return str(imagehash.phash(img))
        except Exception as exc:  # noqa: BLE001
            logger.debug("phash compute failed: %s", exc)
            return None

    def enrich_with_phash(self, enrichment: Optional[dict]) -> Optional[dict]:
        """Compute and inject ``enrichment['phash']`` from any image URL it holds.

        Mutates and returns the same dict. No-op when the enrichment already has
        a pHash, carries no image URL, or the download/hash fails.
        """
        if not isinstance(enrichment, dict) or enrichment.get("phash"):
            return enrichment
        url = self._find_image_url(enrichment)
        if not url:
            return enrichment
        phash = self.hash_image_url(url)
        if phash:
            enrichment["phash"] = phash
            enrichment["phash_source"] = url
        return enrichment

    @staticmethod
    def _find_image_url(enrichment: dict) -> Optional[str]:
        for key in IMAGE_URL_KEYS:
            value = enrichment.get(key)
            if isinstance(value, str) and value.strip().lower().startswith("http"):
                return value.strip()
        # Fall back to any nested 'socid' sub-record (socid_extractor output).
        nested = enrichment.get("socid")
        if isinstance(nested, dict):
            for key in IMAGE_URL_KEYS:
                value = nested.get(key)
                if isinstance(value, str) and value.strip().lower().startswith("http"):
                    return value.strip()
        return None
