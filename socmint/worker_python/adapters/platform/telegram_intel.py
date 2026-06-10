"""TelegramIntelAdapter — Tier 4 Telegram username enrichment via Telethon.

Replaces the geogramint/telegramsint shell-outs (geogramint is a PyQt desktop
GUI with no CLI; neither tool was provisioned into the image). This adapter
talks to Telegram's MTProto API directly with the user-supplied credentials:

    TELEGRAM_API_ID, TELEGRAM_API_HASH  -> from https://my.telegram.org/apps
    TELEGRAM_SESSION                     -> a Telethon StringSession produced by
                                            scripts/telegram_session_login.py
                                            (one-time interactive phone+OTP login)

For a discovered @handle it resolves the public profile: numeric user id,
username, first/last name, bio ("about"), verified/scam/fake/premium flags and
— when Telegram exposes it — the phone number. All run inside the worker with
no GUI and no interactive prompts; if the session is missing or invalid the
adapter degrades gracefully to 'unavailable'.
"""
from __future__ import annotations

import asyncio
import os

from worker_python.adapters.base import ToolAdapter
from api.models.evidence import EvidenceUnit


class TelegramIntelAdapter(ToolAdapter):
    """Resolves a Telegram username to its public profile via Telethon."""

    def name(self) -> str:
        return "telegram_intel"

    def version(self) -> str:
        return "telethon"

    def get_tool_tier(self) -> int:
        return 4

    def get_proxy_tier(self) -> int:
        # Direct egress: MTProto over a Tor SOCKS proxy needs explicit Telethon
        # proxy wiring and is commonly throttled; the session cookie is the auth.
        return 2

    def health_check(self) -> bool:
        if not (
            os.environ.get("TELEGRAM_API_ID")
            and os.environ.get("TELEGRAM_API_HASH")
            and os.environ.get("TELEGRAM_SESSION")
        ):
            return False
        try:
            import telethon  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def _extract_username(seed: str) -> str:
        s = seed.strip().strip("/")
        for marker in ("t.me/", "telegram.me/", "telegram.dog/"):
            if marker in s:
                s = s.split(marker, 1)[1].strip("/")
                break
        return s.lstrip("@").split("/", 1)[0]

    async def _resolve(self, username: str) -> dict | None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl.functions.users import GetFullUserRequest

        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]
        session = os.environ["TELEGRAM_SESSION"]

        client = TelegramClient(StringSession(session), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return None
            entity = await client.get_entity(username)
            data: dict = {
                "user_id": getattr(entity, "id", None),
                "username": getattr(entity, "username", None),
                "first_name": getattr(entity, "first_name", None),
                "last_name": getattr(entity, "last_name", None),
                "phone": getattr(entity, "phone", None),
                "is_bot": getattr(entity, "bot", None),
                "is_verified": getattr(entity, "verified", None),
                "is_scam": getattr(entity, "scam", None),
                "is_fake": getattr(entity, "fake", None),
                "is_premium": getattr(entity, "premium", None),
            }
            try:
                full = await client(GetFullUserRequest(entity))
                data["about"] = getattr(full.full_user, "about", None)
            except Exception:  # noqa: BLE001 — bio is best-effort
                pass
            return data
        finally:
            await client.disconnect()

    def run(self, seed: str) -> list[dict]:
        username = self._extract_username(seed)
        if not username:
            return []
        try:
            data = asyncio.run(self._resolve(username))
        except Exception:  # noqa: BLE001 — graceful degradation in base.execute
            return []
        return [data] if data else []

    def parse(self, raw: list[dict]) -> list[EvidenceUnit]:
        units: list[EvidenceUnit] = []
        for data in raw:
            handle = data.get("username") or str(data.get("user_id") or "")
            units.append(
                self.make_evidence(
                    source_platform="telegram",
                    source_tier=2,
                    seed_value="",
                    result_type="account_found",
                    result_value=f"https://t.me/{handle}" if data.get("username") else handle,
                    platform_enrichment=data,
                    notes="telegram profile (telethon)",
                )
            )
        return units
