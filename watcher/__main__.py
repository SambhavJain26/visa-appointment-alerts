"""CLI entry point.

  python -m watcher run [--dry-run|--live]     run the watcher (default command)
  python -m watcher login                      one-time interactive Telegram login
  python -m watcher test-ai "text" [--image f] run the AI check on sample input
  python -m watcher test-filter "text"         show the NA filter decision
  python -m watcher bot-chats                  list chat IDs that pressed Start on your alert bot
  python -m watcher test-alert ["text"]        send one message to each alert target
  python -m watcher replay [--limit N]         show decisions for recent group messages
                                               (read-only, no alerts, no state changes)
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import mimetypes
import os
import sys
from pathlib import Path

from .config import load_config


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="watcher")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    lg = sub.add_parser("login")
    lg.add_argument("--send-code", action="store_true",
                    help="non-interactive step 1: ask Telegram to send a login code")
    lg.add_argument("--code", help="non-interactive step 2: finish login with the received code")
    t = sub.add_parser("test-ai")
    t.add_argument("text", nargs="?", default="")
    t.add_argument("--image", action="append", default=[])
    f = sub.add_parser("test-filter")
    f.add_argument("text")
    w = sub.add_parser("test-alert", aliases=["test-whatsapp"])
    w.add_argument("--all", action="store_true", help="send to EVERY recipient (default: owner only)")
    w.add_argument("text", nargs="?", default="hello from the H-1B slot watcher 👋")
    sub.add_parser("bot-chats", help="list chats that messaged your alert bot (to get chat IDs)")
    tm = sub.add_parser("test-message",
                        help="run old group posts (t.me links) through the pipeline and send as TEST alerts")
    tm.add_argument("links", nargs="+")
    tm.add_argument("--no-send", action="store_true")
    tm.add_argument("--all", action="store_true", help="send to EVERY recipient (default: owner only)")
    rp = sub.add_parser("replay")
    rp.add_argument("--limit", type=int, default=50)
    rp.add_argument("--no-ai", action="store_true")
    args = p.parse_args()

    cfg = load_config()
    cmd = args.cmd or "run"
    if cmd == "run":
        if args.dry_run:
            cfg = dataclasses.replace(cfg, dry_run=True)
        if args.live:
            cfg = dataclasses.replace(cfg, dry_run=False)
        from .app import run
        try:
            asyncio.run(run(cfg))
        except asyncio.CancelledError:
            pass  # controlled SIGTERM: app cleanup has closed the browser/profile
    elif cmd == "login":
        if args.send_code or args.code:
            asyncio.run(_login_two_step(cfg, args.code))
        else:
            asyncio.run(_login(cfg))
    elif cmd == "test-ai":
        asyncio.run(_test_ai(cfg, args.text, args.image))
    elif cmd == "test-filter":
        from .na_filter import classify
        print(classify(args.text))
    elif cmd in ("test-alert", "test-whatsapp"):
        asyncio.run(_test_alert(cfg, args.text, args.all))
    elif cmd == "bot-chats":
        asyncio.run(_bot_chats(cfg))
    elif cmd == "test-message":
        asyncio.run(_test_message(cfg, args.links, not args.no_send, args.all))
    elif cmd == "replay":
        asyncio.run(_replay(cfg, args.limit, not args.no_ai))


def _session_copy_client(cfg):
    """Telethon client using a temp copy of the session file, so CLI tools can run while the
    live watcher holds the real session open."""
    import shutil
    import tempfile

    from .telegram_poller import make_client

    src = Path(str(cfg.tg_session) + ".session")
    if not src.exists():
        sys.exit("Not logged in. Run: python -m watcher login")
    tmpdir = Path(tempfile.mkdtemp(prefix="watcher-cli-"))
    dst = tmpdir / "cli.session"
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    client = make_client(tmpdir / "cli", cfg.tg_api_id, cfg.tg_api_hash)
    client._cli_tmpdir = tmpdir
    return client


async def _close_copy_client(client) -> None:
    import shutil

    await client.disconnect()
    shutil.rmtree(getattr(client, "_cli_tmpdir", ""), ignore_errors=True)


async def _login(cfg) -> None:
    from .telegram_poller import make_client

    if not (cfg.tg_api_id and cfg.tg_api_hash):
        sys.exit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
    client = make_client(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash)
    # Interactive: prompts for phone (if not in .env), login code, and 2FA password if enabled.
    await client.start(phone=lambda: cfg.tg_phone or input("Phone (with +country code): "))
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved.")
    print("IMPORTANT: now restart the watcher so it loads the new login: "
          "docker compose up -d --force-recreate")
    await client.disconnect()
    for path in (str(cfg.tg_session), str(cfg.tg_session) + ".session"):
        if os.path.exists(path):
            os.chmod(path, 0o600)
            print(f"Secured {path} (chmod 600)")


async def _login_two_step(cfg, code: str | None) -> None:
    """Login without an interactive terminal: --send-code, then --code 12345.
    If 2FA is on, put TELEGRAM_2FA_PASSWORD in .env for step 2 (remove it afterwards)."""
    from telethon import errors

    from .telegram_poller import make_client

    if not (cfg.tg_api_id and cfg.tg_api_hash and cfg.tg_phone):
        sys.exit("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE in .env first.")
    hash_file = cfg.data_dir / "login_code_hash"
    client = make_client(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Already logged in.")
            return
        if code is None:
            sent = await client.send_code_request(cfg.tg_phone)
            hash_file.write_text(sent.phone_code_hash)
            print("Code sent. Check your Telegram app, then run: login --code <CODE>")
            return
        if not hash_file.exists():
            sys.exit("Run login --send-code first.")
        try:
            await client.sign_in(phone=cfg.tg_phone, code=code.strip(),
                                 phone_code_hash=hash_file.read_text().strip())
        except errors.SessionPasswordNeededError:
            pw = os.getenv("TELEGRAM_2FA_PASSWORD")
            if not pw:
                sys.exit("2FA is on: add TELEGRAM_2FA_PASSWORD to .env and run login --code again "
                         "(with a fresh code if this one expired).")
            await client.sign_in(password=pw)
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (id {me.id}). Session saved.")
        print("IMPORTANT: now restart the watcher so it loads the new login: "
              "docker compose up -d --force-recreate")
        hash_file.unlink(missing_ok=True)
    finally:
        await client.disconnect()
        for path in (str(cfg.tg_session), str(cfg.tg_session) + ".session"):
            if os.path.exists(path):
                os.chmod(path, 0o600)


async def _test_ai(cfg, text: str, images: list[str]) -> None:
    from .ai_check import AIChecker
    from .na_filter import classify

    imgs = []
    for path in images:
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        imgs.append((Path(path).read_bytes(), mime))
    fr = classify(text, has_image=bool(imgs))
    print(f"NA filter: {'SKIP' if fr.skip else 'KEEP'} ({fr.reason})")
    if fr.skip:
        return
    res = await AIChecker(cfg.openai_api_key, cfg.openai_model, cfg.ai_timeout, cfg.ai_image_timeout,
                   cfg.openai_fallback_model).check(text, imgs)
    print(f"AI ({cfg.openai_model}): availability={res.availability} failed={res.failed}")
    print(f"   type={res.appointment_type} location={res.location!r} dates={res.dates!r}")
    print(f"   summary: {res.summary}")


def _test_targets(cfg, everyone: bool):
    """Test messages go to the owner (first alert chat) only, unless --all is given,
    so family members never receive test messages by accident."""
    from .alerts import build_targets

    targets = build_targets(cfg)
    if not everyone:
        targets = targets[:1]
    print("test recipients:", [t.label for t in targets])
    return targets


async def _test_alert(cfg, text: str, everyone: bool = False) -> None:
    from .alerts import AlertSender, build_targets

    targets = _test_targets(cfg, everyone)
    if not targets:
        sys.exit("No alert targets: set TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHATS in .env")
    import html

    sender = AlertSender(targets, dry_run=False)
    for t in targets:
        ok, detail = await sender.send_now(t, html.escape(text))
        print(f"{t.label}: {'OK' if ok else 'FAILED'} - {detail}")
    await sender.aclose()


async def _test_message(cfg, links: list[str], send: bool, everyone: bool = False) -> None:
    """Fetch specific (old) posts, run filter + AI, and send the real alert format, marked TEST."""
    import re as _re

    from .ai_check import AIChecker
    from .alerts import (AlertSender, SlotAlert, build_targets, clean_group_title, html_to_plain,
                         render_alert)
    from .na_filter import classify
    from .telegram_poller import download_image, to_tg_message

    client = _session_copy_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Not logged in. Run: python -m watcher login")
    ai = AIChecker(cfg.openai_api_key, cfg.openai_model, cfg.ai_timeout, cfg.ai_image_timeout,
                   cfg.openai_fallback_model)
    sender = AlertSender(_test_targets(cfg, everyone), dry_run=False)
    for link in links:
        m = _re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
        if not m:
            print(f"{link}: not a t.me/<group>/<id> link")
            continue
        group, mid = m.group(1), int(m.group(2))
        ent = await client.get_entity(group)
        raw = await client.get_messages(ent, ids=mid)
        if raw is None:
            print(f"{link}: message not found")
            continue
        msg = to_tg_message(raw, group, getattr(ent, "title", group))
        fr = classify(msg.text, has_image=msg.has_image,
                      is_service=msg.is_service, is_sticker=msg.is_sticker)
        print(f"\n{link}\n  text: {msg.text[:120]!r}\n  filter: {'SKIP' if fr.skip else 'KEEP'} ({fr.reason})")
        if fr.skip:
            continue
        imgs = []
        if msg.has_image:
            img = await download_image(client, msg, cfg.max_image_bytes)
            if img:
                imgs.append(img)
        res = await ai.check(msg.text, imgs)
        print(f"  ai: availability={res.availability} type={res.appointment_type} "
              f"location={res.location!r} dates={res.dates!r} summary={res.summary!r}")
        if not res.availability:
            continue
        text = render_alert(SlotAlert(
            appointment_type=res.appointment_type, location=res.location, dates=res.dates,
            summary=res.summary, original_text=msg.text,
            source=clean_group_title(msg.group_title), link=msg.link,
            link_label="Open the post in Telegram", seen=msg.date, zones=cfg.timezones,
            ai_failed=res.failed, has_image=msg.has_image))
        text = "🧪 <i>TEST · old message, not a live slot</i>\n\n" + text
        print("  alert:\n    " + html_to_plain(text).replace("\n", "\n    "))
        if send:
            for t in sender.targets:
                ok, detail = await sender.send_now(t, text, preview=msg.has_image)
                print(f"  sent to {t.label}: {'OK' if ok else 'FAILED ' + detail}")
            await asyncio.sleep(2)
    await sender.aclose()
    await _close_copy_client(client)


async def _bot_chats(cfg) -> None:
    """Show private chats that sent /start to the bot, so you can copy the chat ID."""
    import httpx

    if not cfg.tg_bot_token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first.")
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(f"https://api.telegram.org/bot{cfg.tg_bot_token}/getUpdates")
        data = resp.json()
        me = (await http.get(f"https://api.telegram.org/bot{cfg.tg_bot_token}/getMe")).json()
    if not data.get("ok"):
        sys.exit(f"Telegram error: {data.get('description')}")
    print(f"Bot: @{(me.get('result') or {}).get('username')}")
    seen = {}
    for u in data.get("result", []):
        chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat")
        if chat:
            seen[chat["id"]] = chat
    if not seen:
        print("No chats yet. Open your bot in Telegram, press Start (or send /start), then rerun.")
    for cid, c in seen.items():
        who = c.get("first_name") or c.get("title") or ""
        print(f"  chat_id={cid}  type={c.get('type')}  name={who}  @{c.get('username', '')}")


async def _replay(cfg, limit: int, use_ai: bool) -> None:
    """Read the last N messages of each group and print what would be forwarded."""
    from .ai_check import AIChecker
    from .na_filter import classify
    from .telegram_poller import download_image, make_client, to_tg_message
    from .timeutil import IST

    client = _session_copy_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Not logged in. Run: python -m watcher login")
    ai = AIChecker(cfg.openai_api_key, cfg.openai_model, cfg.ai_timeout, cfg.ai_image_timeout,
                   cfg.openai_fallback_model)
    totals = {"read": 0, "skip_filter": 0, "skip_ai": 0, "forward": 0}
    for g in cfg.tg_groups:
        try:
            ent = await client.get_entity(g)
            msgs = await client.get_messages(ent, limit=limit)
        except Exception as e:
            print(f"\n=== @{g}: UNREADABLE - {type(e).__name__}: {e}")
            continue
        title = getattr(ent, "title", g)
        print(f"\n=== @{g} ({title}) - last {len(msgs)} messages")
        for m in reversed(msgs):
            msg = to_tg_message(m, g, title)
            totals["read"] += 1
            fr = classify(msg.text, has_image=msg.has_image,
                          is_service=msg.is_service, is_sticker=msg.is_sticker)
            when = msg.date.astimezone(IST).strftime("%d %b %H:%M IST")
            short = " ".join(msg.text.split())[:70]
            if fr.skip:
                totals["skip_filter"] += 1
                print(f"  {when} skip  [{fr.reason}] {short!r}")
                continue
            if not use_ai:
                print(f"  {when} -> AI  {'[img] ' if msg.has_image else ''}{short!r}")
                continue
            imgs = []
            if msg.has_image:
                img = await download_image(client, msg, cfg.max_image_bytes)
                if img:
                    imgs.append(img)
            res = await ai.check(msg.text, imgs)
            if res.availability:
                totals["forward"] += 1
                print(f"  {when} FORWARD {'[img] ' if msg.has_image else ''}{short!r}\n"
                      f"          -> {res.summary}  {msg.link}")
            else:
                totals["skip_ai"] += 1
                print(f"  {when} skip  [ai] {'[img] ' if msg.has_image else ''}{short!r} -> {res.summary}")
        await asyncio.sleep(3)
    await _close_copy_client(client)
    print(f"\nTotals: {totals}")


if __name__ == "__main__":
    main()
