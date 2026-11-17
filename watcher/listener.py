"""Reads the channels in one Telegram folder with the user account. Never sends anything."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter, DialogFilterChatlist

from .pipeline import Pipeline, Post

log = logging.getLogger(__name__)


class FolderNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class Channel:
    title: str
    username: str | None


def filter_title(folder) -> str:
    """Folder titles are TextWithEntities in current layers and plain strings in older ones."""
    title = getattr(folder, "title", "")
    return getattr(title, "text", title) or ""


def find_folder(filters, name: str):
    wanted = name.strip().casefold()
    for folder in filters:
        if isinstance(folder, (DialogFilter, DialogFilterChatlist)) and filter_title(folder).strip().casefold() == wanted:
            return folder
    titles = [filter_title(f) for f in filters if isinstance(f, (DialogFilter, DialogFilterChatlist))]
    raise FolderNotFound(f"no folder named {name!r}; folders on this account: {titles}")


MAX_LINKS = 10


def extract_links(message) -> list[tuple[str, str]]:
    """(label, url) for links the plain text hides: text links ("Apply" pointing somewhere)
    and URL buttons under the post. Works with old and new Telegram API layers."""
    links: list[tuple[str, str]] = []
    try:
        for entity, label in message.get_entities_text() or []:
            url = getattr(entity, "url", None)  # MessageEntityTextUrl only
            if url:
                links.append((label.strip(), url))
    except Exception:
        pass
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None) or getattr(getattr(button, "type", None), "url", None)
            if url:
                links.append(((getattr(button, "text", "") or "").strip(), url))
    seen, unique = set(), []
    for label, url in links:
        if url not in seen:
            seen.add(url)
            unique.append((label or "link", url))
    return unique[:MAX_LINKS]


def text_with_links(message) -> str:
    text = message.message or ""
    hidden = [(label, url) for label, url in extract_links(message) if url not in text]
    if not hidden:
        return text
    return text + "\n\nLinks in the post:\n" + "\n".join(f"- {label}: {url}" for label, url in hidden)


class Listener:
    def __init__(self, settings, pipeline: Pipeline, store):
        self.settings = settings
        self.pipeline = pipeline
        self.store = store
        self.channels: dict[int, Channel] = {}
        self.client = TelegramClient(
            StringSession(settings.session_string),
            settings.api_id,
            settings.api_hash,
            auto_reconnect=True,
            connection_retries=-1,  # keep retrying; the outer loop handles anything worse
            retry_delay=5,
            flood_sleep_threshold=120,  # sleep through short flood waits automatically
            catch_up=True,  # replay updates missed while disconnected; dedupe stops repeats
        )
        self._tasks: set[asyncio.Task] = set()

    # --- folder ---------------------------------------------------------------

    async def resolve_folder(self) -> dict[int, Channel]:
        response = await self.client(GetDialogFiltersRequest())
        filters = getattr(response, "filters", response)
        folder = find_folder(filters, self.settings.folder_name)

        peers = list(folder.pinned_peers) + list(folder.include_peers)
        excluded = {utils.get_peer_id(p) for p in getattr(folder, "exclude_peers", [])}
        entities = await self.client.get_entity(peers) if peers else []

        # A folder can also include "all channels" or "all groups" as a rule.
        if getattr(folder, "broadcasts", False) or getattr(folder, "groups", False):
            async for dialog in self.client.iter_dialogs():
                if (folder.broadcasts and dialog.is_channel and not dialog.is_group) or (
                    folder.groups and dialog.is_group
                ):
                    entities.append(dialog.entity)

        channels: dict[int, Channel] = {}
        for entity in entities:
            peer_id = utils.get_peer_id(entity)
            if peer_id in excluded:
                continue
            channels[peer_id] = Channel(
                title=utils.get_display_name(entity) or str(peer_id),
                username=getattr(entity, "username", None),
            )
        return channels

    async def refresh_folder(self) -> None:
        try:
            channels = await self.resolve_folder()
        except FloodWaitError as exc:
            log.warning("flood wait %ss while refreshing the folder", exc.seconds)
            await asyncio.sleep(exc.seconds)
            return
        added = channels.keys() - self.channels.keys()
        removed = self.channels.keys() - channels.keys()
        self.channels = channels
        if added or removed or not channels:
            log.info(
                "folder %r: %d chats (+%d -%d): %s",
                self.settings.folder_name,
                len(channels),
                len(added),
                len(removed),
                ", ".join(c.title for c in channels.values()),
            )

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.folder_refresh_seconds)
            try:
                await self.refresh_folder()
            except FolderNotFound as exc:
                log.error("%s; keeping the previous channel list", exc)
            except Exception:
                log.exception("folder refresh failed; keeping the previous channel list")

    # --- messages -------------------------------------------------------------

    def to_post(self, message, chat_id: int) -> Post | None:
        if not (message.message or "").strip():
            return None  # pure media, or an album item without the caption
        text = text_with_links(message)  # text or caption, plus links hidden behind words or buttons
        channel = self.channels.get(chat_id) or Channel(title=str(chat_id), username=None)
        return Post(
            chat_id=chat_id,
            msg_id=message.id,
            text=text,
            channel_title=channel.title,
            username=channel.username,
        )

    async def _on_new_message(self, event) -> None:
        # Only new posts: edits arrive as MessageEdited, which is not subscribed, so they never re-alert.
        post = self.to_post(event.message, event.chat_id)
        if post is None:
            return
        try:
            await self.pipeline.handle(post)
        except Exception:
            log.exception("failed to handle %s/%s", post.chat_id, post.msg_id)

    async def sync_history(self) -> None:
        """On start: fetch posts missed while the watcher was down (for chats it has seen
        before), and on the very first start backfill BACKFILL_PER_CHANNEL posts per chat."""
        first_start = not self.store.get_meta("started_once")
        backfill = self.settings.backfill_per_channel if first_start else 0
        for chat_id in list(self.channels):
            last_seen = self.store.last_msg_id(chat_id)
            if last_seen is not None:
                kwargs = {"min_id": last_seen, "limit": 50}
            elif backfill > 0:
                kwargs = {"limit": backfill}
            else:
                continue
            try:
                messages = [m async for m in self.client.iter_messages(chat_id, **kwargs)]
            except FloodWaitError as exc:
                log.warning("flood wait %ss while reading history", exc.seconds)
                await asyncio.sleep(exc.seconds)
                continue
            except Exception:
                log.exception("reading history failed for %s", chat_id)
                continue
            if messages:
                log.info("history: %d posts from %s", len(messages), self.channels[chat_id].title)
            for message in reversed(messages):  # oldest first
                post = self.to_post(message, chat_id)
                if post:
                    await self.pipeline.handle(post)
        self.store.set_meta("started_once", "1")

    async def scan_recent(self, hours: int, per_chat_limit: int = 200) -> Counter:
        """Run the last `hours` of every chat through the pipeline (on request, via /scan).
        Posts already seen are skipped by the store, so nothing is scored twice."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        counts: Counter = Counter()
        for chat_id in list(self.channels):
            try:
                messages = []
                async for message in self.client.iter_messages(chat_id, limit=per_chat_limit):
                    if message.date < cutoff:
                        break
                    messages.append(message)
            except FloodWaitError as exc:
                log.warning("flood wait %ss during scan", exc.seconds)
                await asyncio.sleep(exc.seconds)
                continue
            except Exception:
                log.exception("scan failed for %s", chat_id)
                continue
            for message in reversed(messages):  # oldest first, one at a time to spare rate limits
                post = self.to_post(message, chat_id)
                if post:
                    counts[await self.pipeline.handle(post)] += 1
        log.info("scan of %sh done: %s", hours, dict(counts))
        return counts

    # --- lifecycle ------------------------------------------------------------

    def is_connected(self) -> bool:
        return self.client.is_connected()

    async def run(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise PermissionError("the Telegram session string is not authorised (revoked or wrong)")
        me = await self.client.get_me()
        log.info("logged in as user id %s", me.id)

        await self.refresh_folder()
        self.client.add_event_handler(
            self._on_new_message,
            # No incoming=True: posts you make yourself in a test channel must count too.
            events.NewMessage(func=lambda e: e.chat_id in self.channels),
        )
        refresher = asyncio.create_task(self._refresh_loop())
        self._tasks.add(refresher)
        try:
            await self.sync_history()
            await self.client.run_until_disconnected()
        finally:
            refresher.cancel()
            self._tasks.discard(refresher)
            self.client.remove_event_handler(self._on_new_message)
