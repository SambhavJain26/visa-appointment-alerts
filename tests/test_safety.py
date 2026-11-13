"""Static safety checks: the running service must stay read-only and send-only."""
import re
from pathlib import Path

SERVICE_FILES = ["app.py", "telegram_poller.py", "alerts.py", "cvs_poller.py", "ai_check.py",
                 "na_filter.py", "db.py", "health.py", "config.py"]
SRC = {f: (Path(__file__).parent.parent / "watcher" / f).read_text(encoding="utf-8")
       for f in SERVICE_FILES}


def test_bot_never_reads_incoming_messages():
    # No getUpdates / webhook in the running service: what users type to the bot is never read.
    for name, code in SRC.items():
        assert "getUpdates" not in code, name
        assert "setWebhook" not in code, name


def test_user_account_never_writes_to_telegram():
    forbidden = [r"\.send_message\(", r"\.send_file\(", r"JoinChannel", r"ImportChatInvite",
                 r"\.send_read_acknowledge\(", r"SendReaction", r"\.forward_messages\(",
                 r"\.delete_messages\(", r"\.edit_message\("]
    for name, code in SRC.items():
        for pat in forbidden:
            assert not re.search(pat, code), f"{pat} found in {name}"


def test_never_touches_visa_portal():
    for name, code in SRC.items():
        low = code.lower()
        assert "usvisascheduling" not in low and "ustraveldocs" not in low, name


def test_bot_only_sends():
    calls = set(re.findall(r'/bot\{self\.token\}/(\w+)', SRC["alerts.py"]))
    assert calls == {"sendMessage", "sendPhoto"}


def test_images_never_written_to_disk():
    code = SRC["telegram_poller.py"]
    assert "download_media(m, file=bytes)" in code  # in-memory download
    assert not re.search(r"download_media\([^)]*file=(?!bytes)", code)
    for name, src in SRC.items():
        assert "write_bytes(" not in src, name
        assert not re.search(r"open\([^)]*['\"]wb['\"]", src), name
