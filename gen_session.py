#!/usr/bin/env python3
"""Make a Telethon session string for the watcher. Run this on your laptop, not the server.

    python gen_session.py            # QR login (scan from Telegram on your phone)
    python gen_session.py --phone    # phone number + login code instead
    python gen_session.py --out session.txt   # write the string to a 0600 file, not the screen

You need TELEGRAM_API_ID and TELEGRAM_API_HASH from https://my.telegram.org (API development
tools). They can come from the environment or the prompts below.

The string this prints is full access to your Telegram account. Treat it like a password.
To revoke it: Telegram > Settings > Devices > terminate the "job-watcher" session.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


def show_qr(url: str) -> None:
    try:
        import qrcode  # optional: pip install qrcode
    except ImportError:
        print("\nInstall `qrcode` to draw the QR here (pip install qrcode), or turn this URL into a QR")
        print("code with any offline tool and scan it:\n")
        print(url)
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


async def qr_login(client: TelegramClient) -> None:
    print("Telegram on your phone: Settings > Devices > Link Desktop Device, then scan:\n")
    login = await client.qr_login()
    while True:
        show_qr(login.url)
        try:
            await login.wait(timeout=60)
            return
        except asyncio.TimeoutError:
            print("\nThe code expired, here is a new one.\n")
            await login.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Two-step verification password: "))
            return


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", action="store_true", help="log in with phone number and code instead of QR")
    parser.add_argument("--out", type=Path, help="write the session string to this file (mode 0600)")
    args = parser.parse_args()

    api_id = os.environ.get("TELEGRAM_API_ID") or input("api_id: ").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH") or getpass.getpass("api_hash (hidden): ").strip()

    client = TelegramClient(
        StringSession(),
        int(api_id),
        api_hash,
        device_model="job-watcher",  # the name you will see under Settings > Devices
        app_version="1.0",
        receive_updates=False,
    )
    await client.connect()
    try:
        if args.phone:
            await client.start()  # prompts for phone, code and 2FA password
        else:
            await qr_login(client)
        me = await client.get_me()
        session = client.session.save()
    finally:
        await client.disconnect()

    print(f"\nLogged in as {me.first_name} (user id {me.id}).")
    print(f"NOTIFY_CHAT_ID={me.id}   <- your chat id with any bot is your user id")
    if args.out:
        args.out.touch(mode=0o600, exist_ok=True)
        args.out.chmod(0o600)
        args.out.write_text(session + "\n")
        print(f"Session string written to {args.out} (mode 0600).")
    else:
        print("\nTELEGRAM_SESSION_STRING (copy it into the server's .env, then clear your scrollback):\n")
        print(session)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
