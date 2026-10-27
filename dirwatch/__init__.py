"""Watch local directories and post new files to Matrix rooms.

The plugin uses inotify (through :mod:`asyncinotify`) to notice files that
appear in or are written to a configured directory. A file counts as settled
when no further change event arrives for :data:`STABILITY_SECONDS` seconds. The
plugin then renders the file according to its extension, posts the result to
every room mapped to the directory, and moves the file into one of three
subdirectories.

Each watched directory gets three subdirectories:

* ``done/``: files that were posted successfully.
* ``failed/``: files that were recognised but could not be posted, even after
  retrying.
* ``skipped/``: files the plugin does not recognise.

A post that raises an error is retried once per entry in :data:`RETRY_DELAYS`
before the file is marked as failed.

Every moved file keeps its name and gains a UTC ``YYYY-MM-DD-HHMMSS-`` prefix.

Supported extensions:

* ``.md``: rendered as Markdown into a formatted HTML message.
* ``.txt``: posted verbatim as a plain-text message.
* ``.png``, ``.jpg``, ``.jpeg``, ``.gif``, ``.webp``, ``.svg``: uploaded and
  posted as an image.

Every other file is moved to ``skipped/``.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

import markdown2
from asyncinotify import Event, Inotify, Mask
from maubot import Plugin  # type: ignore[attr-defined]
from mautrix.types import ImageInfo, RoomID
from mautrix.util.config import BaseProxyConfig

from .config import Config

#: Seconds a file must stay unchanged before the plugin processes it.
STABILITY_SECONDS = 3

#: Seconds to wait before each retry after a failed post.
RETRY_DELAYS = (5, 30, 120)

#: Matrix events are limited to about 64 KB. Stay below that.
MAX_BYTES = 62_000

#: Extensions the plugin renders as Markdown.
MARKDOWN_SUFFIXES = frozenset({".md"})

#: Extensions the plugin posts verbatim.
TEXT_SUFFIXES = frozenset({".txt"})

#: Extensions the plugin uploads as images.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})

#: MIME types for the image extensions.
IMAGE_MIMETYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

#: inotify events that mean "a writer finished with a file".
#:
#: ``CLOSE_WRITE`` fires when a file opened for writing is closed, and
#: ``MOVED_TO`` fires when a complete file is renamed into the directory. Both
#: are completion signals, unlike ``CREATE``, which fires while the file is
#: still empty. The settling timer still debounces repeated closes.
WATCH_MASK = Mask.CLOSE_WRITE | Mask.MOVED_TO

#: Subdirectory names for each processing outcome.
DONE_DIRNAME = "done"
FAILED_DIRNAME = "failed"
SKIPPED_DIRNAME = "skipped"

#: Format of the timestamp prefix added to every moved file, in UTC.
TIMESTAMP_FORMAT = "%Y-%m-%d-%H%M%S"


def truncate_at_line(text: str, limit: int) -> str:
    """Trim text to at most `limit` bytes, cutting at a line boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    cut = encoded[:limit].decode("utf-8", errors="ignore")
    newline = cut.rfind("\n")
    return cut[:newline] if newline != -1 else cut


def _snapshot(path: Path) -> tuple[int, int] | None:
    """Return `(size, mtime_ns)` for `path`, or None if it is not a file."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _timestamped(path: Path) -> Path:
    """Return `path` with a UTC timestamp prefix, avoiding collisions.

    A numeric suffix is appended when the timestamped name is already taken,
    so no existing file is ever overwritten.
    """
    stamp = datetime.now(UTC).strftime(TIMESTAMP_FORMAT)
    candidate = path.with_name(f"{stamp}-{path.name}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{stamp}-{counter}-{path.name}")
        counter += 1
    return candidate


def render_markdown(text: str) -> str:
    """Render Markdown `text` into the HTML subset Matrix supports."""
    return markdown2.markdown(  # type: ignore[no-any-return]
        text,
        extras=["fenced-code-blocks", "tables", "strike"],
    )


class DirWatchBot(Plugin):
    """Watch directories and forward settled files to Matrix rooms."""

    directories: dict[Path, list[RoomID]]
    timers: dict[Path, asyncio.Task[None]]
    task: asyncio.Task[None] | None

    async def start(self) -> None:
        """Load the config, install watches, and start the event loop."""
        await super().start()
        config = self.config
        assert config is not None
        config.load_and_update()
        self.directories = {}
        self.timers = {}
        self.task = None
        self._load_directories()
        self.task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Cancel the event loop and any pending settling timers."""
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        for timer in self.timers.values():
            timer.cancel()
        self.timers.clear()
        await super().stop()

    def _load_directories(self) -> None:
        """Read the directory mapping from the config."""
        config = self.config
        assert config is not None
        for raw_path, rooms in config["directories"].items():
            path = Path(raw_path)
            if not path.is_dir():
                self.log.warning("ignoring %s: not a directory", path)
                continue
            self.directories[path] = [RoomID(room) for room in rooms]
            self.log.info("watching %s for %s", path, ", ".join(rooms))

    async def _watch_loop(self) -> None:
        """Read inotify events forever and arm settling timers."""
        with Inotify() as inotify:
            for directory in self.directories:
                inotify.add_watch(directory, WATCH_MASK)
            async for event in inotify:
                self._on_event(event)

    def _on_event(self, event: Event) -> None:
        """Arm or re-arm the settling timer for the file in `event`."""
        path = event.path
        if path is None or not path.is_file():
            return
        directory = self._owning_directory(path)
        if directory is None:
            return
        self._arm(path, directory)

    def _owning_directory(self, path: Path) -> Path | None:
        """Return the configured directory that contains `path`, if any."""
        for directory in self.directories:
            if path.parent == directory:
                return directory
        return None

    async def _settle(self, path: Path, directory: Path) -> None:
        """Wait for `path` to stop changing, then process it.

        The file's size and modification time are recorded before the wait and
        checked again afterwards. A writer that is still appending keeps the
        timer alive, so a partially written file is never processed.
        """
        try:
            stamp = _snapshot(path)
            if stamp is None:
                return
            await asyncio.sleep(STABILITY_SECONDS)
            if _snapshot(path) != stamp:
                self._arm(path, directory)
                return
            outcome = await self._post_with_retries(path, directory)
            self._dispose(path, directory, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - keep the watcher alive
            self.log.exception("failed to post %s: %s", path, err)
        finally:
            if self.timers.get(path) is asyncio.current_task():
                self.timers.pop(path, None)

    async def _post_with_retries(self, path: Path, directory: Path) -> str:
        """Post `path`, retrying with backoff, and return the outcome.

        A post that keeps failing is retried once per entry in
        :data:`RETRY_DELAYS`. When every attempt fails, the last error is
        logged and the file is marked as failed.
        """
        attempts = len(RETRY_DELAYS) + 1
        last_error: Exception | None = None
        for index in range(attempts):
            if index:
                delay = RETRY_DELAYS[index - 1]
                self.log.warning(
                    "retrying %s in %ds (attempt %d/%d)",
                    path,
                    delay,
                    index + 1,
                    attempts,
                )
                await asyncio.sleep(delay)
                if not path.is_file():
                    self.log.info("%s disappeared during retries", path)
                    return SKIPPED_DIRNAME
            try:
                return await self._process(path, directory)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - retried below
                last_error = err
                self.log.warning(
                    "attempt %d/%d for %s failed: %s", index + 1, attempts, path, err
                )
        self.log.error(
            "gave up on %s after %d attempts, last error: %s",
            path,
            attempts,
            last_error,
        )
        return FAILED_DIRNAME

    def _arm(self, path: Path, directory: Path) -> None:
        """Replace any existing settling timer for `path`."""
        timer = self.timers.pop(path, None)
        if timer is not None:
            timer.cancel()
        self.timers[path] = asyncio.create_task(self._settle(path, directory))

    async def _process(self, path: Path, directory: Path) -> str:
        """Post `path` to every room of `directory`, returning the outcome."""
        suffix = path.suffix.lower()
        if suffix in MARKDOWN_SUFFIXES:
            return await self._post_markdown(path, directory)
        if suffix in TEXT_SUFFIXES:
            return await self._post_text(path, directory)
        if suffix in IMAGE_SUFFIXES:
            return await self._post_image(path, directory)
        self.log.info("unsupported extension, skipping %s", path)
        return SKIPPED_DIRNAME

    def _dispose(self, path: Path, directory: Path, outcome: str) -> None:
        """Move `path` into the `outcome` subdirectory of `directory`."""
        if not path.exists():
            return
        target_dir = directory / outcome
        try:
            target_dir.mkdir(exist_ok=True)
            path.rename(_timestamped(target_dir / path.name))
        except OSError as err:
            self.log.warning("cannot move %s to %s: %s", path, target_dir, err)

    async def _post_markdown(self, path: Path, directory: Path) -> str:
        """Send `path` as a formatted HTML message, returning the outcome."""
        source = truncate_at_line(path.read_text(encoding="utf-8"), MAX_BYTES)
        if not source.strip():
            self.log.info("skipping empty file %s", path)
            return SKIPPED_DIRNAME
        html = render_markdown(source)
        for room_id in self.directories[directory]:
            await self.client.send_text(room_id, text=source, html=html)
        return DONE_DIRNAME

    async def _post_text(self, path: Path, directory: Path) -> str:
        """Send the contents of `path` as a plain-text message."""
        text = truncate_at_line(path.read_text(encoding="utf-8"), MAX_BYTES)
        if not text.strip():
            self.log.info("skipping empty file %s", path)
            return SKIPPED_DIRNAME
        for room_id in self.directories[directory]:
            await self.client.send_text(room_id, text=text)
        return DONE_DIRNAME

    async def _post_image(self, path: Path, directory: Path) -> str:
        """Upload `path` and send it as an image message."""
        suffix = path.suffix.lower()
        data = path.read_bytes()
        mime_type = IMAGE_MIMETYPES[suffix]
        mxc = await self.client.upload_media(
            data,
            mime_type=mime_type,
            filename=path.name,
        )
        info = ImageInfo(mimetype=mime_type, size=len(data))  # type: ignore[call-arg,no-untyped-call]
        for room_id in self.directories[directory]:
            await self.client.send_image(room_id, mxc, info=info, file_name=path.name)
        return DONE_DIRNAME

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        """Return the config class for this plugin."""
        return Config
