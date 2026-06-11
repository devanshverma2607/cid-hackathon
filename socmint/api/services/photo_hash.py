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
import math
import os
from functools import lru_cache
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

# --- CLIP reverse-image embedding (optional, graceful) ----------------------
# A perceptual hash only links byte-near-identical avatars. A CLIP image
# embedding additionally links the SAME picture after re-encoding, resizing,
# light cropping or recolouring (and visually identical avatars), feeding the
# W_PHOTO_MATCH correlation/persona signal and a pgvector reverse-image store.
# It reuses the already-installed sentence-transformers + torch + Pillow stack,
# so it adds no new dependency; the model (~600 MB) downloads to the Hugging
# Face cache on first use. Disable entirely with CLIP_IMAGE_EMBEDDINGS=0.
CLIP_MODEL_NAME = os.getenv("CLIP_MODEL_NAME", "clip-ViT-B-32")
CLIP_EMBED_DIM = 512
CLIP_ENABLED = os.getenv("CLIP_IMAGE_EMBEDDINGS", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)

# --- Face-recognition embedding (optional, graceful) ------------------------
# pHash/CLIP match the SAME image; a face embedding matches the SAME PERSON
# across DIFFERENT photos (different pose/lighting/crop). It detects the largest
# face (MTCNN) and embeds it (FaceNet/InceptionResnetV1, vggface2) into an
# L2-normalised 512-d vector compared by cosine. Reuses the installed CPU torch,
# so the only new dependency is facenet-pytorch; model weights (~110 MB) download
# to the torch cache on first use. Disable with FACE_RECOGNITION=0.
FACE_EMBED_DIM = 512
FACE_RECOGNITION_ENABLED = os.getenv("FACE_RECOGNITION", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)


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


@lru_cache(maxsize=1)
def _get_clip_model():
    """Lazily load and cache the CLIP image-embedding model (or None)."""
    try:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(CLIP_MODEL_NAME)
    except Exception as exc:  # noqa: BLE001 — embeddings are best-effort
        logger.warning(
            "CLIP model unavailable (%s); photo matching falls back to pHash", exc
        )
        return None


@lru_cache(maxsize=1)
def _get_face_models():
    """Lazily load and cache the MTCNN detector + FaceNet embedder (or None)."""
    try:
        from facenet_pytorch import MTCNN, InceptionResnetV1

        detector = MTCNN(image_size=160, margin=14, post_process=True,
                         select_largest=True, device="cpu")
        embedder = InceptionResnetV1(pretrained="vggface2").eval()
        return (detector, embedder)
    except Exception as exc:  # noqa: BLE001 — face matching is best-effort
        logger.warning(
            "face models unavailable (%s); face matching disabled", exc
        )
        return None


class PhotoHasher:
    """Download profile images and compute perceptual hashes (pHash)."""

    def hash_image_url(self, url: str) -> Optional[str]:
        """Return the hex pHash of the image at ``url`` (or None on any failure)."""
        data = self._download_image(url)
        return self.hash_image_bytes(data) if data else None

    def _download_image(self, url: str) -> Optional[bytes]:
        """SSRF-guarded streamed download of an image (or None on any failure)."""
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
            return data or None
        except Exception as exc:  # noqa: BLE001 — download is best-effort enrichment
            logger.debug("image download failed for %s: %s", url, exc)
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

    @staticmethod
    def embed_image_bytes(data: bytes) -> Optional[list[float]]:
        """Compute a CLIP reverse-image embedding for raw image bytes (or None)."""
        if not CLIP_ENABLED:
            return None
        model = _get_clip_model()
        if model is None:
            return None
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as img:
                vector = model.encode(img.convert("RGB"))
            return [round(float(x), 6) for x in vector]
        except Exception as exc:  # noqa: BLE001
            logger.debug("CLIP embed failed: %s", exc)
            return None

    @staticmethod
    def embed_face_bytes(data: bytes) -> Optional[list[float]]:
        """Detect the largest face and return an L2-normalised FaceNet embedding.

        Returns None when face recognition is disabled, the models are
        unavailable, or no face is detected. Unlike pHash/CLIP (same image), this
        links the SAME PERSON across different photos.
        """
        if not FACE_RECOGNITION_ENABLED:
            return None
        models = _get_face_models()
        if models is None:
            return None
        detector, embedder = models
        try:
            import torch
            from PIL import Image

            with Image.open(BytesIO(data)) as img:
                face = detector(img.convert("RGB"))
            if face is None:
                return None
            with torch.no_grad():
                vec = embedder(face.unsqueeze(0))[0].tolist()
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            return [round(x / norm, 6) for x in vec]
        except Exception as exc:  # noqa: BLE001
            logger.debug("face embed failed: %s", exc)
            return None

    @staticmethod
    def _gps_decimal(dms, ref) -> Optional[float]:
        """Convert an EXIF GPS (degrees, minutes, seconds) tuple to a signed float."""
        try:
            d, m, s = (float(x) for x in (dms[0], dms[1], dms[2]))
            val = d + m / 60.0 + s / 3600.0
            if str(ref).strip().upper() in ("S", "W"):
                val = -val
            return val
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def extract_exif(data: bytes) -> Optional[dict]:
        """Pull GPS coordinates, camera, and capture time from image EXIF.

        A geotagged image is often the single most decisive geolocation lead in
        a case. ``.convert('RGB')`` (used for hashing/embedding) strips metadata,
        so this must run on the original bytes. Returns any of ``gps {lat,lon}``,
        ``camera``, ``captured_at`` — or None when no useful metadata is present.
        """
        try:
            from PIL import ExifTags, Image

            with Image.open(BytesIO(data)) as img:
                exif = img.getexif()
            if not exif:
                return None
            out: dict = {}
            cam = " ".join(
                str(exif.get(t)).strip() for t in (271, 272) if exif.get(t)
            ).strip()
            if cam:
                out["camera"] = cam[:120]
            captured = None
            try:
                ex_ifd = exif.get_ifd(ExifTags.IFD.Exif)
                captured = ex_ifd.get(36867) or ex_ifd.get(36868)
            except Exception:  # noqa: BLE001
                captured = None
            captured = captured or exif.get(306)
            if captured:
                out["captured_at"] = str(captured).strip()[:25]
            try:
                gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
            except Exception:  # noqa: BLE001
                gps = None
            if gps:
                lat = PhotoHasher._gps_decimal(gps.get(2), gps.get(1))
                lon = PhotoHasher._gps_decimal(gps.get(4), gps.get(3))
                if (lat is not None and lon is not None
                        and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
                        and not (lat == 0.0 and lon == 0.0)):
                    out["gps"] = {"lat": round(lat, 6), "lon": round(lon, 6)}
            return out or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("exif extract failed: %s", exc)
            return None

    def enrich_with_phash(self, enrichment: Optional[dict]) -> Optional[dict]:
        """Inject ``enrichment['phash']`` (+ CLIP ``image_embedding`` + ``exif``)
        from any image URL the enrichment holds.

        Downloads the avatar once and computes the perceptual hash, a CLIP
        reverse-image embedding (when enabled), and any EXIF metadata (GPS /
        camera / capture time). Mutates and returns the same dict. No-op when all
        are present, no image URL is found, or the download fails. The signals
        degrade independently.
        """
        if not isinstance(enrichment, dict):
            return enrichment
        need_phash = not enrichment.get("phash")
        need_embed = CLIP_ENABLED and not enrichment.get("image_embedding")
        need_face = FACE_RECOGNITION_ENABLED and "face_embedding" not in enrichment
        need_exif = "exif" not in enrichment
        if not need_phash and not need_embed and not need_face and not need_exif:
            return enrichment
        url = enrichment.get("phash_source") or self._find_image_url(enrichment)
        if not url:
            return enrichment
        data = self._download_image(url)
        if not data:
            return enrichment
        if need_phash:
            phash = self.hash_image_bytes(data)
            if phash:
                enrichment["phash"] = phash
                enrichment["phash_source"] = url
        if need_embed:
            embedding = self.embed_image_bytes(data)
            if embedding:
                enrichment["image_embedding"] = embedding
        if need_face:
            face = self.embed_face_bytes(data)
            # Sentinel [] records "checked, no face" so we never re-download.
            enrichment["face_embedding"] = face if face else []
        if need_exif:
            exif = self.extract_exif(data)
            if exif:
                enrichment["exif"] = exif
                gps = exif.get("gps")
                if gps:
                    # Flat key so it surfaces in the evidence table + is easy to mine.
                    enrichment["exif_gps"] = f"{gps['lat']},{gps['lon']}"
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
