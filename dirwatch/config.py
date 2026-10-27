"""Configuration for the dirwatch plugin."""

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    """Per-instance configuration for :class:`dirwatch.DirWatchBot`."""

    def do_update(self, helper: ConfigUpdateHelper) -> None:
        """Copy the watcher mapping from the user config into the base config."""
        helper.copy("directories")
