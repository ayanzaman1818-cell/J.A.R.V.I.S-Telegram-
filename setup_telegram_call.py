"""One-time interactive login script for JARVIS's Native Telegram VoIP Calling account.

Supports BOTH:
  1. QR Code Login (Recommended — zero SMS/code waiting! Just scan from Telegram on your phone: Settings -> Devices -> Link Desktop Device)
  2. Phone Number + Code Login (with automatic resend via SMS/Call fallback)

Run:
    py setup_telegram_call.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import config


def _render_qr_ascii(url: str) -> None:
    """Render a scannable QR code in the terminal and also open a clean PNG image on screen."""
    try:
        import urllib.parse
        import urllib.request

        #Also generate a clean local HTML/PNG QR preview window so it's effortless to scan
        qr_html = Path(config.BASE_DIR) / "telegram_qr_login.html"
        encoded = urllib.parse.quote(url, safe="")
        qr_html.write_text(
            f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>Scan with Telegram (Settings → Devices → Link Desktop Device)</title>
<style>
  body {{
    background: #0b0f19; color: #f8fafc; font-family: Segoe UI, sans-serif;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    height: 100vh; margin: 0; text-align: center;
  }}
  .card {{
    background: #161f30; padding: 32px 40px; border-radius: 24px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.1);
  }}
  .qr-box {{
    background: #ffffff; padding: 18px; border-radius: 16px; display: inline-block; margin: 18px 0;
  }}
  h2 {{ margin: 0 0 8px; color: #38bdf8; }}
  p {{ color: #94a3b8; max-width: 380px; line-height: 1.5; margin: 6px auto; }}
</style>
</head>
<body>
  <div class="card">
    <h2>J.A.R.V.I.S. Telegram Call Login</h2>
    <p>1. Open <b>Telegram</b> on your phone (logged into JARVIS's account)</p>
    <p>2. Go to <b>Settings → Devices → Link Desktop Device</b></p>
    <p>3. Point your phone camera at this QR code:</p>
    <div class="qr-box">
      <img src="https://api.qrserver.com/v1/create-qr-code/?size=260x260&data={encoded}" width="260" height="260" alt="QR Code" />
    </div>
    <p style="font-size:12px;color:#64748b;">This window can be closed once the terminal says SUCCESS.</p>
  </div>
</body>
</html>""",
            encoding="utf-8",
        )
        import webbrowser

        webbrowser.open(qr_html.as_uri())
        print(f"\n  [Opened QR Code in your browser: {qr_html}]")
    except Exception as exc:
        print(f"  (Could not open browser QR preview: {exc})")


async def _login_with_qr(client) -> bool:
    """Log in via Telegram QR Code (Settings -> Devices -> Link Desktop Device)."""
    from telethon.errors import SessionPasswordNeededError

    print("\n" + "-" * 68)
    print("  QR CODE LOGIN")
    print("  1. Open Telegram on your phone (on the account you want JARVIS to use)")
    print("  2. Tap Settings -> Devices -> Link Desktop Device")
    print("  3. Scan the QR code that just opened in your browser!")
    print("-" * 68)

    qr_login = await client.qr_login()
    _render_qr_ascii(qr_login.url)

    while True:
        try:
            await qr_login.wait(timeout=35)
            return True
        except SessionPasswordNeededError:
            pw = input("\n  2FA Cloud Password is enabled on this account. Enter your 2FA password: ").strip()
            await client.sign_in(password=pw)
            return True
        except asyncio.TimeoutError:
            print("  Refreshing QR code...")
            await qr_login.recreate()
            _render_qr_ascii(qr_login.url)


async def _login_with_phone(client) -> bool:
    """Log in with phone number and explicitly show where the code was sent + offer SMS/Call resend."""
    from telethon.errors import SessionPasswordNeededError

    phone = input("\n  Enter phone number with country code (e.g. +8801xxxxxxxxx): ").strip()
    if not phone:
        return False

    sent = await client.send_code_request(phone)
    code_type = type(sent.type).__name__
    print(f"\n  -> Telegram sent the code via: {code_type}")
    if "App" in code_type:
        print(
            "  ℹ️  'SentCodeTypeApp' means Telegram sent the code as a chat message INSIDE the Telegram app\n"
            "     (from the official 'Telegram' service notification chat on any device already logged into that number),\n"
            "     NOT as a carrier SMS!"
        )
    print("  (If you didn't get it, type 'sms' and press Enter to request an SMS/Call resend, or type 'qr' to switch to QR Code scan.)")

    while True:
        code = input("\n  Enter the 5-digit code (or type 'sms' / 'qr'): ").strip()
        if code.lower() == "qr":
            return await _login_with_qr(client)
        if code.lower() == "sms":
            try:
                sent = await client.send_code_request(phone, force_sms=True)
                print(f"  -> Requested resend! New delivery method: {type(sent.type).__name__}")
            except Exception as exc:
                print(f"  -> Telegram refused SMS resend ({exc}). Use QR Code login instead!")
            continue
        if not code:
            continue
        try:
            await client.sign_in(phone=phone, code=code)
            return True
        except SessionPasswordNeededError:
            pw = input("  Enter your 2-Step Verification (2FA) password: ").strip()
            await client.sign_in(password=pw)
            return True
        except Exception as exc:
            print(f"  Sign-in error: {exc}")


async def main() -> None:
    from telethon import TelegramClient

    print("=" * 68)
    print("  JARVIS — Native Telegram VoIP Call Account Setup")
    print("=" * 68)

    session_path = str(config.TELEGRAM_CALL_SESSION)
    if session_path.endswith(".session"):
        session_path = session_path[: -len(".session")]

    client = TelegramClient(
        session_path,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
        device_model="JARVIS AI Workstation",
        system_version="Windows 11",
        app_version="5.8.1 x64",
    )

    await client.connect()
    if not await client.is_user_authorized():
        print(
            "\nChoose how to log in JARVIS's Telegram account:\n"
            "  [1] Scan QR Code from Telegram on your phone (Recommended — instant, no waiting for SMS/codes!)\n"
            "  [2] Enter Phone Number & 5-digit Code"
        )
        choice = input("\nSelect 1 or 2 [default: 1]: ").strip() or "1"
        if choice == "2":
            await _login_with_phone(client)
        else:
            await _login_with_qr(client)

    me = await client.get_me()
    qr_html = Path(config.BASE_DIR) / "telegram_qr_login.html"
    if qr_html.exists():
        try:
            qr_html.unlink()
        except Exception:
            pass

    print("\n" + "=" * 68)
    print(f"  SUCCESS! Logged in as: {me.first_name} (@{me.username or 'no_username'}) [ID: {me.id}]")
    print(f"  Session saved to:      {session_path}.session")
    print("=" * 68)

    if config.OWNER_ID and me.id == config.OWNER_ID:
        print(
            "\n⚠️  NOTE: You logged in with the SAME Telegram account as OWNER_ID.\n"
            "Telegram does not allow an account to place a 1-on-1 call to itself.\n"
            "To receive 1-on-1 calls from JARVIS, log in here with a 2nd Telegram account."
        )
    else:
        print(
            "\nNext step:\n"
            "  1. Open Telegram on your main account and send a quick 'Hi' message to\n"
            f"     this JARVIS account (@{me.username or me.first_name}) so they are in each other's contacts.\n"
            "  2. Run `py main.py` and type `/call` (or tap the native 📞 Call button on JARVIS's profile)!"
        )

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
