"""What happens to one post: pre-filter, dedupe, score, decide, notify."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .notifier import Dispatcher, format_alert
from .prefilter import reject_reason
from .scorer import ScoreError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Post:
    chat_id: int  # Telethon "marked" id: -100… for channels and supergroups
    msg_id: int
    text: str
    channel_title: str
    username: str | None = None


def post_link(post: Post) -> str:
    if post.username:
        return f"https://t.me/{post.username}/{post.msg_id}"
    marked = str(post.chat_id)
    if marked.startswith("-100"):
        return f"https://t.me/c/{marked[4:]}/{post.msg_id}"
    return ""  # basic groups have no message links


class Pipeline:
    def __init__(
        self,
        store,
        scorer,
        dispatcher: Dispatcher,
        markers: list[re.Pattern[str]],
        fit_threshold: int = 70,
        min_post_chars: int = 200,
        max_concurrent: int = 4,
    ):
        self.store = store
        self.scorer = scorer
        self.dispatcher = dispatcher
        self.markers = markers
        self.fit_threshold = fit_threshold
        self.min_post_chars = min_post_chars
        self._slots = asyncio.Semaphore(max_concurrent)

    async def handle(self, post: Post) -> str:
        """Returns the outcome, for logs, summaries and tests."""
        outcome = await self._handle(post)
        self.store.record_outcome(outcome, post.chat_id, post.msg_id, post_link(post), post.channel_title)
        return outcome

    async def _handle(self, post: Post) -> str:
        reason = reject_reason(post.text, self.min_post_chars, self.markers)
        if reason:
            log.debug("skip %s/%s: %s", post.chat_id, post.msg_id, reason)
            return f"skipped:{reason}"

        claim = self.store.claim(post.chat_id, post.msg_id, post.text)
        if claim != "new":
            log.info("skip %s/%s: %s", post.chat_id, post.msg_id, claim)
            return claim

        link = post_link(post)
        try:
            async with self._slots:
                result = await self.scorer.score(post.text, post.channel_title)
        except ScoreError as exc:
            log.warning("could not score %s: %s", link or post.msg_id, exc)
            self.store.record_error(post.chat_id, post.msg_id, link, post.channel_title, str(exc))
            return "error"

        self.store.record_result(post.chat_id, post.msg_id, link, post.channel_title, result)
        score, eligible = result["fit_score"], result["eligible"]
        log.info(
            "scored %s: job=%s score=%s eligible=%s title=%r",
            link or post.msg_id,
            result["is_job_posting"],
            score,
            eligible,
            result.get("title", ""),
        )

        if not result["is_job_posting"]:
            return "not_job"
        if eligible == "no":
            return "ineligible"
        if score < self.fit_threshold:
            return "below_threshold"

        sent_now = await self.dispatcher.deliver(format_alert(result, link, post.channel_title))
        self.store.mark_alerted(post.chat_id, post.msg_id)
        return "alerted" if sent_now else "queued"
