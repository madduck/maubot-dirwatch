"""Tests for the dirwatch plugin."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest
from mautrix.types import ContentURI, RoomID

from dirwatch import (
    IMAGE_MIMETYPES,
    MAX_BYTES,
    DirWatchBot,
    render_markdown,
    truncate_at_line,
)


class FakeClient:
    """Record calls the plugin makes to the Matrix client."""

    def __init__(self) -> None:
        self.texts: list[dict[str, object]] = []
        self.uploads: list[dict[str, object]] = []
        self.images: list[dict[str, object]] = []

    async def send_text(self, room_id: str, **kwargs: object) -> None:
        self.texts.append({"room_id": room_id, **kwargs})

    async def upload_media(self, data: bytes, **kwargs: object) -> ContentURI:
        self.uploads.append({"data": data, **kwargs})
        return ContentURI("mxc://example.com/upload")

    async def send_image(self, room_id: str, url: ContentURI, **kwargs: object) -> None:
        self.images.append({"room_id": room_id, "url": url, **kwargs})


class FakeEvent:
    """Minimal stand-in for :class:`asyncinotify.Event`."""

    def __init__(self, path: Path | None) -> None:
        self.path = path


def make_bot(directory: Path, rooms: list[str]) -> tuple[DirWatchBot, FakeClient]:
    """Build a plugin instance wired to a fake client."""
    bot = DirWatchBot.__new__(DirWatchBot)
    client = FakeClient()
    bot.client = client  # type: ignore[assignment]
    bot.log = logging.getLogger("dirwatch-test")  # type: ignore[assignment]
    bot.directories = {directory: [RoomID(room) for room in rooms]}
    bot.timers = {}
    bot.task = None
    bot.loop = asyncio.get_event_loop()
    return bot, client


def test_truncate_at_line_keeps_short_text() -> None:
    assert truncate_at_line("hello", MAX_BYTES) == "hello"


def test_truncate_at_line_cuts_at_newline() -> None:
    text = "\n".join(["line"] * 100)
    result = truncate_at_line(text, 50)
    assert len(result.encode("utf-8")) <= 50
    assert not result.endswith("lin")


def test_render_markdown_produces_html() -> None:
    html = render_markdown("# Title\n\n**bold**")
    assert "<h1>" in html
    assert "<strong>bold</strong>" in html


async def test_on_event_ignores_missing_path(tmp_path: Path) -> None:
    bot, _client = make_bot(tmp_path, ["!room:example.com"])
    bot._on_event(FakeEvent(None))
    assert bot.timers == {}


async def test_on_event_ignores_foreign_directory(tmp_path: Path) -> None:
    bot, _client = make_bot(tmp_path, ["!room:example.com"])
    outsider = tmp_path.parent / "other" / "note.txt"
    bot._on_event(FakeEvent(outsider))
    assert bot.timers == {}


async def test_on_event_arms_timer(tmp_path: Path) -> None:
    bot, _client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "note.txt"
    target.write_text("hello")
    bot._on_event(FakeEvent(target))
    assert target in bot.timers
    bot.timers[target].cancel()


async def test_process_posts_text(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "note.txt"
    target.write_text("hello")
    await bot._process(target, tmp_path)
    assert client.texts == [{"room_id": "!room:example.com", "text": "hello"}]
    assert not target.exists()


async def test_process_posts_markdown_as_html(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "note.md"
    target.write_text("# Title")
    await bot._process(target, tmp_path)
    assert len(client.texts) == 1
    assert client.texts[0]["text"] == "# Title"
    assert "<h1>Title</h1>" in str(client.texts[0]["html"])
    assert not target.exists()


async def test_process_uploads_image(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "pic.png"
    target.write_bytes(b"\x89PNG")
    await bot._process(target, tmp_path)
    assert client.uploads == [
        {"data": b"\x89PNG", "mime_type": "image/png", "filename": "pic.png"}
    ]
    assert client.images[0]["room_id"] == "!room:example.com"
    assert client.images[0]["file_name"] == "pic.png"
    assert not target.exists()


async def test_process_ignores_unknown_extension(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "data.bin"
    target.write_bytes(b"\x00\x01")
    await bot._process(target, tmp_path)
    assert client.texts == []
    assert client.uploads == []
    assert target.exists()


async def test_process_posts_to_every_room(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!a:example.com", "!b:example.com"])
    target = tmp_path / "note.txt"
    target.write_text("hi")
    await bot._process(target, tmp_path)
    assert [entry["room_id"] for entry in client.texts] == [
        "!a:example.com",
        "!b:example.com",
    ]


async def test_process_skips_empty_file_and_deletes(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "empty.txt"
    target.write_text("   ")
    await bot._process(target, tmp_path)
    assert client.texts == []
    assert not target.exists()


async def test_settle_processes_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0)
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "note.txt"
    target.write_text("hello")
    await bot._settle(target, tmp_path)
    assert client.texts == [{"room_id": "!room:example.com", "text": "hello"}]
    assert target not in bot.timers


async def test_settle_skips_deleted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0)
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "gone.txt"
    target.write_text("hello")
    target.unlink()
    await bot._settle(target, tmp_path)
    assert client.texts == []


async def test_image_mimetypes_cover_suffixes() -> None:
    assert IMAGE_MIMETYPES[".svg"] == "image/svg+xml"
    assert set(IMAGE_MIMETYPES) == {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
    }


async def test_watch_loop_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0.05)
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    task = asyncio.create_task(bot._watch_loop())
    try:
        await asyncio.sleep(0.1)
        (tmp_path / "note.txt").write_text("hello")
        for _ in range(200):
            if client.texts:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert client.texts == [{"room_id": "!room:example.com", "text": "hello"}]
    assert not (tmp_path / "note.txt").exists()
