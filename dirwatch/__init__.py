"""Watch local directories and post new files to Matrix rooms.

The plugin uses inotify (through :mod:`asyncinotify`) to notice files that
appear in or are written to a configured directory. A file counts as settled
when no further change event arrives for :data:`STABILITY_SECONDS` seconds. The
plugin then renders the file according to its extension, posts the result to
every room mapped to the directory, and deletes the file.

Supported extensions:

* ``.md``: rendered as Markdown into a formatted HTML message.
* ``.txt``: posted verbatim as a plain-text message.
* ``.png``, ``.jpg``, ``.jpeg``, ``.gif``, ``.webp``, ``.svg``: uploaded and
  posted as an image.

Every other file is left alone.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import markdown2
from asyncinotify import Event, Inotify, Mask
from maubot import Plugin  # type: ignore[attr-defined]
from mautrix.types import ImageInfo, RoomID
from mautrix.util.config import BaseProxyConfig

from .config import Config

#: Seconds a file must stay unchanged before the plugin processes it.
STABILITY_SECONDS = 3

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

#: inotify events that mean "a file may have just been written".
WATCH_MASK = Mask.CLOSE_WRITE | Mask.MOVED_TO | Mask.CREATE


def truncate_at_line(text: str, limit: int) -> str:
    """Trim text to at most `limit` bytes, cutting at a line boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    cut = encoded[:limit].decode("utf-8", errors="ignore")
    newline = cut.rfind("\n")
    return cut[:newline] if newline != -1 else cut


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
        timer = self.timers.pop(path, None)
        if timer is not None:
            timer.cancel()
        self.timers[path] = asyncio.create_task(self._settle(path, directory))

    def _owning_directory(self, path: Path) -> Path | None:
        """Return the configured directory that contains `path`, if any."""
        for directory in self.directories:
            if path.parent == directory:
                return directory
        return None

    async def _settle(self, path: Path, directory: Path) -> None:
        """Wait for `path` to stop changing, then process it."""
        try:
            await asyncio.sleep(STABILITY_SECONDS)
            if path.is_file():
                await self._process(path, directory)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - keep the watcher alive
            self.log.exception("failed to post %s: %s", path, err)
        finally:
            if self.timers.get(path) is asyncio.current_task():
                self.timers.pop(path, None)

    async def _process(self, path: Path, directory: Path) -> None:
        """Post `path` to every room of `directory`, then delete it."""
        suffix = path.suffix.lower()
        try:
            if suffix in MARKDOWN_SUFFIXES:
                await self._post_markdown(path, directory)
            elif suffix in TEXT_SUFFIXES:
                await self._post_text(path, directory)
            elif suffix in IMAGE_SUFFIXES:
                await self._post_image(path, directory)
            else:
                return
        except OSError as err:
            self.log.warning("cannot process %s: %s", path, err)
            return
        try:
            path.unlink()
        except OSError as err:
            self.log.warning("cannot delete %s: %s", path, err)

    async def _post_markdown(self, path: Path, directory: Path) -> None:
        """Send `path` as a formatted HTML message."""
        source = truncate_at_line(path.read_text(encoding="utf-8"), MAX_BYTES)
        if not source.strip():
            self.log.info("skipping empty file %s", path)
            return
        html = render_markdown(source)
        for room_id in self.directories[directory]:
            await self.client.send_text(room_id, text=source, html=html)

    async def _post_text(self, path: Path, directory: Path) -> None:
        """Send the contents of `path` as a plain-text message."""
        text = truncate_at_line(path.read_text(encoding="utf-8"), MAX_BYTES)
        if not text.strip():
            self.log.info("skipping empty file %s", path)
            return
        for room_id in self.directories[directory]:
            await self.client.send_text(room_id, text=text)

    async def _post_image(self, path: Path, directory: Path) -> None:
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

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        """Return the config class for this plugin."""
        return Config
