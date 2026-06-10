"""One-time Telegram session generator (run interactively).

Telegram username/phone resolution requires a *user* session (a bot token cannot
resolve arbitrary @handles). This script performs the one-time interactive login
and prints a Telethon StringSession that the worker reuses non-interactively.

HOW TO RUN (from the repo root, in your own terminal):

    docker compose exec -it worker_python python /app/worker_python/telegram_session_login.py

You will be asked for:
  1. your phone number (international format, e.g. +9198XXXXXXXX)
  2. the login code Telegram sends to your Telegram app
  3. your 2FA password, only if you have two-step verification enabled

It then prints a long TELEGRAM_SESSION string. Copy it into .env:

    TELEGRAM_SESSION=<the printed value>

then reload the workers:

    docker compose up -d --force-recreate worker_python

The session string is a CREDENTIAL equivalent to being logged into your account.
Keep it out of version control and revoke it from Telegram > Settings > Devices
when you are done testing.
"""
import asyncio
import os
import sys

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("telethon is not installed in this image. Rebuild worker_python first.")


async def _login() -> None:
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not (api_id and api_hash):
        sys.exit("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set in the environment.")

    print("Logging in to Telegram to mint a reusable session string...\n")
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    # start() prompts for phone -> login code -> 2FA password (if enabled).
    await client.start()

    # Save and print the session FIRST so nothing afterward can lose it.
    session_string = client.session.save()
    print("\n" + "=" * 70)
    print("Copy the line below into your .env file:\n")
    print(f"TELEGRAM_SESSION={session_string}")
    print("=" * 70)

    try:
        me = await client.get_me()
        if me is not None:
            name = getattr(me, "username", None) or getattr(me, "first_name", None) or "unknown"
            print(f"\nLogged in as: {name} (id={getattr(me, 'id', '?')})")
    except Exception:  # noqa: BLE001 — display only; session already printed
        pass

    await client.disconnect()
    print("\nThen run: docker compose up -d --force-recreate worker_python")


def main() -> None:
    asyncio.run(_login())


if __name__ == "__main__":
    main()
