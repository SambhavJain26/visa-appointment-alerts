from datetime import datetime, timezone

import pytest

from watcher.ai_check import parse_ai_output
from watcher.alerts import (SlotAlert, clean_group_title, format_time, html_to_plain,
                            render_alert)
from watcher.cvs_poller import BlockedError, changed_rows, describe_row, parse_cvs, row_fields

HEAD = (
    "<tr><th><div>Visa Location<svg></svg></div></th><th>Visa Type</th><th>Earliest Date</th>"
    "<th>Slots on Earliest Date</th><th><span>Total Dates<br>Available</span></th>"
    "<th>Last Seen At</th><th>Relative Time</th></tr>"
)


def _row(*cells):
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


IST_TABLE = "<table>" + HEAD + _row(
    "CHENNAI", "H-1B (Regular)", "09 Nov, 27", "1", "1", "06 Nov 2026, 12:26 AM", "18 hrs ago"
) + _row(
    "CHENNAI VAC", "H-1B (Regular)", "30 Nov, 26", "6", "1", "06 Nov 2026, 01:31 PM", "5 hrs ago"
) + "</table>"
UTC_TABLE = "<table>" + HEAD + _row(
    "CHENNAI", "H-1B (Regular)", "09 Nov, 27", "1", "1", "05 Nov 2026, 06:56 PM", "18 hrs ago"
) + "</table>"
GEN = "<p>The current info is generated at 06 Nov 2026, 07:12 PM</p>"
PAGE = f"<html><head><title>Latest US Visa Availability</title></head><body>{GEN}{IST_TABLE}{UTC_TABLE}</body></html>"
PAGE_NOW = datetime(2026, 11, 6, 13, 42, tzinfo=timezone.utc)  # = 07:12 PM IST
SEEN = datetime(2026, 9, 30, 7, 36, tzinfo=timezone.utc)  # 1:06 PM IST


def test_parse_uses_first_table():
    rows = parse_cvs(PAGE, PAGE_NOW)
    assert set(rows) == {"CHENNAI", "CHENNAI VAC"}
    assert rows["CHENNAI"]["last_seen"] == "06 Nov 2026, 12:26 AM"
    assert rows["CHENNAI VAC"]["total_dates"] == "1"


def test_checkpoint_detected():
    with pytest.raises(BlockedError):
        parse_cvs("<html><head><title>Vercel Security Checkpoint</title></head></html>")


def test_changes_and_fields():
    old = parse_cvs(PAGE, PAGE_NOW)
    assert changed_rows({}, old) == []  # first fetch: no alerts
    new = {k: dict(v) for k, v in old.items()}
    assert changed_rows(old, new) == []
    new["CHENNAI VAC"]["last_seen"] = "06 Nov 2026, 07:40 PM"
    ch = changed_rows(old, new)
    assert [r["location"] for r in ch] == ["CHENNAI VAC"]
    f = row_fields(ch[0])
    assert f["appointment_type"] == "biometrics" and f["location"] == "Chennai"
    assert row_fields(old["CHENNAI"])["appointment_type"] == "interview"
    assert "Chennai" in describe_row(ch[0])


def _alert(**kw):
    base = dict(appointment_type="biometrics", location="Hyderabad",
                dates="28 Oct 2026 · 9:00, 9:30", summary="x",
                original_text="H1B slots showing for OFC - October 28 2026, HYD location",
                source="Strictly H1/H4 REGULAR visa slots",
                link="https://t.me/StrictlyH1_H4_regular_visa_slots/93749",
                link_label="Open the post in Telegram", seen=SEEN,
                zones=["Asia/Kolkata", "America/New_York"])
    base.update(kw)
    return SlotAlert(**base)


def test_render_biometrics():
    out = render_alert(_alert())
    lines = out.split("\n")
    assert lines[0] == "🚨 <b>BIOMETRICS (OFC) SLOT</b>"
    assert "📍 <b>Hyderabad</b>" in lines
    assert "📅 28 Oct 2026 · 9:00, 9:30" in lines
    assert "🕒 Posted 30 Sep, 1:06 PM IST · 3:36 AM EDT" in lines
    assert '<a href="https://t.me/StrictlyH1_H4_regular_visa_slots/93749">' in out
    assert "45 min" in out


def test_render_interview_has_no_ofc_reminder():
    out = render_alert(_alert(appointment_type="interview"))
    assert out.startswith("🚨 <b>INTERVIEW SLOT</b>")
    assert "45 min" not in out


def test_render_unknown_and_missing_fields():
    out = render_alert(_alert(appointment_type="weird", location="", dates=""))
    assert "type not clear" in out.split("\n")[0]
    assert "Location not mentioned" in out and "Date not mentioned" in out


def test_render_ai_failed():
    out = render_alert(_alert(ai_failed=True))
    assert out.startswith("⚠️ <b>CHECK THIS POST</b>")
    assert "📍" not in out  # no AI fields when AI failed
    assert "<i>H1B slots showing" in out


def test_render_escapes_html():
    out = render_alert(_alert(original_text="<script>alert(1)</script> & <b>x</b>",
                              location="<Chennai>"))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "&lt;Chennai&gt;" in out


def test_render_image_only():
    out = render_alert(_alert(original_text="", has_image=True, summary="Calendar with open dates"))
    assert "🖼 <i>Screenshot: Calendar with open dates</i>" in out


def test_helpers():
    assert clean_group_title("H1B/H4 Regular/ER Visa Slots(No discussions)") == \
        "H1B/H4 Regular/ER Visa Slots"
    assert format_time(SEEN, ["Asia/Kolkata"]) == "30 Sep, 1:06 PM IST"
    plain = html_to_plain('🚨 <b>X</b>\n<i>y</i> &amp; <a href="https://t.me/a/1">Open</a>')
    assert plain == "🚨 *X*\n_y_ & Open: https://t.me/a/1"


def test_parse_ai_output():
    r = parse_ai_output('{"availability": true, "appointment_type": "biometrics", '
                        '"location": "Chennai", "dates": "20 Nov", "summary": "Chennai OFC"}')
    assert r and r.availability and r.appointment_type == "biometrics"
    assert r.location == "Chennai" and r.dates == "20 Nov"
    r2 = parse_ai_output('{"availability": true, "appointment_type": "??", "summary": "s"}')
    assert r2 and r2.appointment_type == "unknown" and r2.location == ""
    assert parse_ai_output("not json") is None
    assert parse_ai_output('{"availability": "yes", "summary": "x"}') is None
    assert parse_ai_output(None) is None
