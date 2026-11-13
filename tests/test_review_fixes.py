import asyncio
import dataclasses
from datetime import timedelta

import pytest

from watcher.ai_check import AI_FAILED_SUMMARY, AIChecker, AIResult
from watcher.alerts import AlertSender
from watcher.app import Pipeline, looks_like_sighting
from watcher.config import BotChat, load_config
from watcher.db import Store
from watcher.telegram_poller import TgMessage
from watcher.timeutil import now_utc


def _msg(i, text, has_image=False):
    return TgMessage(group="g", group_title="G", id=i, date=now_utc() - timedelta(seconds=20),
                     text=text, has_image=has_image, is_service=False, is_sticker=False,
                     grouped_id=None)


def test_looks_like_sighting():
    assert looks_like_sighting(_msg(1, "", has_image=True))
    assert looks_like_sighting(_msg(2, "CHN OFC Nov 20 available"))
    assert looks_like_sighting(_msg(3, "hyd ofc slots"))
    assert not looks_like_sighting(_msg(4, "Please post photo"))
    assert not looks_like_sighting(_msg(5, "That's a good one"))
    assert not looks_like_sighting(_msg(6, "THIS IS H1 H4 INPERSON GROUP rules"))


class FailingAI:
    async def check(self, text, images=None):
        return AIResult(True, AI_FAILED_SUMMARY, failed=True)


def test_ai_outage_does_not_spam_family(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = dataclasses.replace(load_config(env_file=str(tmp_path / "none.env")), dry_run=False)
    sender = AlertSender([], dry_run=False)
    sent, feed = [], []
    sender.enqueue_html = lambda h, kind="slot", preview=False, **kw: sent.append(h)

    class F:
        def enqueue_html(self, h, kind="feed", preview=False, silent=False, **kw):
            feed.append(h)

    async def go():
        p = Pipeline(cfg, Store(tmp_path / "o.db"), FailingAI(), sender, feed=F())
        for i, t in enumerate(["Please post photo", "That's a good one", "anyone?",
                               "CHN OFC 20 Nov open"]):
            await p.on_telegram(_msg(10 + i, t))
            while p._tasks:
                await asyncio.sleep(0.01)
    asyncio.run(go())
    assert len(sent) == 1 and "CHECK THIS POST" in sent[0]   # only the plausible one
    assert sum("not alerted" in h for h in feed) == 3        # chatter -> owner's feed only


def test_fallback_model_used_when_main_model_missing(monkeypatch):
    import httpx
    import openai

    calls = []

    class FakeCompletions:
        async def create(self, model, **kw):
            calls.append(model)
            if model == "retired-model":
                raise openai.NotFoundError("model not found",
                                           response=httpx.Response(404, request=httpx.Request("POST", "https://x")),
                                           body=None)

            class R:
                class C:
                    class M:
                        content = '{"availability": false, "appointment_type": "unknown", "location": "", "dates": "", "summary": "chatter"}'
                    message = M()
                choices = [C()]
            return R()

    ai = AIChecker("sk-test", "retired-model", 10, 20, fallback_model="backup-model")
    ai.client.chat.completions = FakeCompletions()
    r = asyncio.run(ai.check("hello"))
    assert calls == ["retired-model", "backup-model"]
    assert r.availability is False and not r.failed


def test_test_commands_default_to_owner_only(tmp_path, monkeypatch):
    from watcher.__main__ import _test_targets
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = dataclasses.replace(load_config(env_file=str(tmp_path / "none.env")), tg_bot_token="1:x",
                              bot_chats=[BotChat("1", "me"), BotChat("2", "brother"),
                                         BotChat("3", "purva")])
    assert [t.chat.name for t in _test_targets(cfg, False)] == ["me"]
    assert len(_test_targets(cfg, True)) == 3
