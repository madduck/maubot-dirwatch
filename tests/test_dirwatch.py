"""Tests for the dirwatch plugin."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest
from asyncinotify import Mask
from mautrix.types import ContentURI, RoomID

from dirwatch import (
    DONE_DIRNAME,
    FAILED_DIRNAME,
    IMAGE_MIMETYPES,
    MAX_BYTES,
    SKIPPED_DIRNAME,
    WATCH_MASK,
    DirWatchBot,
    _timestamped,
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


def filed(directory: Path, outcome: str, name: str) -> Path:
    """Return the single file in `directory/outcome`, failing if absent."""
    entries = sorted((directory / outcome).iterdir())
    assert len(entries) == 1, f"expected one file in {outcome}, got {entries}"
    assert entries[0].name.endswith(f"-{name}")
    return entries[0]


async def test_watch_mask_ignores_create_and_delete() -> None:
    assert WATCH_MASK & Mask.CLOSE_WRITE
    assert WATCH_MASK & Mask.MOVED_TO
    assert not WATCH_MASK & Mask.CREATE
    assert not WATCH_MASK & Mask.DELETE


def test_timestamped_adds_utc_prefix(tmp_path: Path) -> None:
    result = _timestamped(tmp_path / "note.txt")
    assert result.name.endswith("-note.txt")
    assert result.parent == tmp_path
    stamp = result.name.removesuffix("-note.txt")
    assert len(stamp) == len("2026-10-27-122401")
    assert stamp[4] == stamp[7] == "-"


def test_timestamped_avoids_collisions(tmp_path: Path) -> None:
    first = _timestamped(tmp_path / "note.txt")
    first.touch()
    second = _timestamped(tmp_path / "note.txt")
    assert second != first
    assert second.name.endswith("-1-note.txt")


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
    outcome = await bot._process(target, tmp_path)
    assert outcome == DONE_DIRNAME
    assert client.texts == [{"room_id": "!room:example.com", "text": "hello"}]
    assert target.exists()
    bot._dispose(target, tmp_path, outcome)
    assert filed(tmp_path, DONE_DIRNAME, "note.txt").read_text() == "hello"


async def test_process_posts_markdown_as_html(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "note.md"
    target.write_text("# Title")
    outcome = await bot._process(target, tmp_path)
    assert outcome == DONE_DIRNAME
    assert len(client.texts) == 1
    assert client.texts[0]["text"] == "# Title"
    assert "<h1>Title</h1>" in str(client.texts[0]["html"])
    bot._dispose(target, tmp_path, outcome)
    assert filed(tmp_path, DONE_DIRNAME, "note.md").exists()


async def test_process_uploads_image(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "pic.png"
    target.write_bytes(b"\x89PNG")
    outcome = await bot._process(target, tmp_path)
    assert outcome == DONE_DIRNAME
    assert client.uploads == [
        {"data": b"\x89PNG", "mime_type": "image/png", "filename": "pic.png"}
    ]
    assert client.images[0]["room_id"] == "!room:example.com"
    assert client.images[0]["file_name"] == "pic.png"
    bot._dispose(target, tmp_path, outcome)
    assert filed(tmp_path, DONE_DIRNAME, "pic.png").read_bytes() == b"\x89PNG"


async def test_process_reports_unknown_extension_as_skipped(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "data.bin"
    target.write_bytes(b"\x00\x01")
    outcome = await bot._process(target, tmp_path)
    assert outcome == SKIPPED_DIRNAME
    assert client.texts == []
    assert client.uploads == []
    assert target.exists()
    bot._dispose(target, tmp_path, outcome)
    assert filed(tmp_path, SKIPPED_DIRNAME, "data.bin").read_bytes() == b"\x00\x01"


async def test_settle_retries_then_moves_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0)
    monkeypatch.setattr("dirwatch.RETRY_DELAYS", (0, 0, 0))
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    calls = 0

    async def boom(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise RuntimeError("matrix said no")

    client.send_text = boom  # type: ignore[method-assign]
    target = tmp_path / "note.txt"
    target.write_text("hello")
    await bot._settle(target, tmp_path)
    assert calls == 4
    assert not target.exists()
    assert filed(tmp_path, FAILED_DIRNAME, "note.txt").read_text() == "hello"
    assert bot.timers == {}


async def test_settle_succeeds_on_a_later_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0)
    monkeypatch.setattr("dirwatch.RETRY_DELAYS", (0, 0, 0))
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    calls = 0
    real_send = client.send_text

    async def flaky(room_id: str, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("temporary glitch")
        await real_send(room_id, **kwargs)

    client.send_text = flaky  # type: ignore[method-assign]
    target = tmp_path / "note.txt"
    target.write_text("hello")
    await bot._settle(target, tmp_path)
    assert calls == 3
    assert client.texts == [{"room_id": "!room:example.com", "text": "hello"}]
    assert filed(tmp_path, DONE_DIRNAME, "note.txt").exists()


async def test_unsupported_extension_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0)
    monkeypatch.setattr("dirwatch.RETRY_DELAYS", (0, 0, 0))
    bot, _client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "data.bin"
    target.write_bytes(b"\x00\x01")
    await bot._settle(target, tmp_path)
    assert filed(tmp_path, SKIPPED_DIRNAME, "data.bin").exists()


async def test_retry_gives_up_if_the_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0)
    monkeypatch.setattr("dirwatch.RETRY_DELAYS", (0, 0, 0))
    bot, client = make_bot(tmp_path, ["!room:example.com"])

    async def vanish(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        target.unlink()
        raise RuntimeError("matrix said no")

    client.send_text = vanish  # type: ignore[method-assign]
    target = tmp_path / "note.txt"
    target.write_text("hello")
    await bot._settle(target, tmp_path)
    assert not target.exists()
    assert not (tmp_path / FAILED_DIRNAME).exists()
    assert not (tmp_path / DONE_DIRNAME).exists()


async def test_settle_does_not_move_file_under_active_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dirwatch.STABILITY_SECONDS", 0)
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "growing.txt"
    target.write_text("part one\n")
    sizes = iter([(9, 1), (18, 2)])
    monkeypatch.setattr("dirwatch._snapshot", lambda _path: next(sizes, (18, 2)))
    await bot._settle(target, tmp_path)
    assert client.texts == []
    assert target.exists()
    assert target in bot.timers
    bot.timers[target].cancel()
    bot.timers.pop(target, None)


async def test_process_posts_to_every_room(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!a:example.com", "!b:example.com"])
    target = tmp_path / "note.txt"
    target.write_text("hi")
    outcome = await bot._process(target, tmp_path)
    assert outcome == DONE_DIRNAME
    assert [entry["room_id"] for entry in client.texts] == [
        "!a:example.com",
        "!b:example.com",
    ]


async def test_process_moves_empty_file_to_skipped(tmp_path: Path) -> None:
    bot, client = make_bot(tmp_path, ["!room:example.com"])
    target = tmp_path / "empty.txt"
    target.write_text("   ")
    outcome = await bot._process(target, tmp_path)
    assert outcome == SKIPPED_DIRNAME
    assert client.texts == []
    assert target.exists()
    bot._dispose(target, tmp_path, outcome)
    assert filed(tmp_path, SKIPPED_DIRNAME, "empty.txt").exists()


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
    assert filed(tmp_path, DONE_DIRNAME, "note.txt").read_text() == "hello"


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
    assert filed(tmp_path, DONE_DIRNAME, "note.txt").read_text() == "hello"
