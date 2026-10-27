# dirwatch — a maubot plugin

Watch local directories and post new files to Matrix rooms.

## What it does

The plugin watches every directory in its configuration with inotify. A file
counts as settled when no further change event arrives for three seconds. The
plugin then renders the file according to its extension, posts the result to
every room mapped to the directory, and deletes the file.

| Extension | Behaviour |
| --- | --- |
| `.md` | Rendered from Markdown into a formatted HTML message. |
| `.txt` | Posted verbatim as a plain-text message. |
| `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg` | Uploaded and posted as an image. |
| anything else | Ignored. The file stays on disk. |

## Configuration

Each plugin instance reads a `directories` mapping. Every key is an absolute
path to a local directory. Every value is a list of Matrix room IDs. The plugin
posts each settled file to all rooms listed for its directory.

```yaml
directories:
  /var/spool/dirwatch/reports:
    - "!reports:example.com"
  /var/spool/dirwatch/alerts:
    - "!alerts:example.com"
    - "!ops:example.com"
```

## Build

Build the plugin into a `.mbp` file with `mbc build`, or zip the directory:

```sh
zip -9r dirwatch.mbp .
```

## Development

Run the tests and the linters:

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
