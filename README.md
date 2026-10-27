# dirwatch — a maubot plugin

Watch local directories and post new files to Matrix rooms.

## What it does

The plugin watches every directory in its configuration with inotify. The
plugin reacts to `IN_CLOSE_WRITE` and `IN_MOVED_TO`, so it acts when a writer
closes a file, or when a complete file is moved into the directory.

A file counts as settled when no further change event arrives for three
seconds, and its size and modification time stopped changing. The plugin then
renders the file according to its extension, posts the result to every room
mapped to the directory, and moves the file into a subdirectory.

| Extension | Behaviour |
| --- | --- |
| `.md` | Rendered from Markdown into a formatted HTML message. |
| `.txt` | Posted verbatim as a plain-text message. |
| `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg` | Uploaded and posted as an image. |
| anything else | Not posted. |

## Where files go

Each watched directory gets three subdirectories. The plugin creates them when
it first needs them.

| Subdirectory | Meaning |
| --- | --- |
| `done/` | The file was posted to every room. |
| `failed/` | The file was recognised, but every attempt to post it failed. |
| `skipped/` | The file has an unsupported extension, or it was empty. |

## Retries

When posting a file raises an error, the plugin tries again. It makes four
attempts in total: the first one, then three retries after 5, 30, and 120
seconds. A file that still fails after the last attempt goes to `failed/`.

The plugin logs each failed attempt at warning level, and the final giving-up
message with the full traceback.

Retries do not apply to files that are skipped. An unsupported extension and an
empty file are not errors, so the plugin moves those to `skipped/` at once.

Every moved file keeps its name and gains a UTC timestamp prefix in the form
`YYYY-MM-DD-HHMMSS-`. A file named `report.txt` becomes
`2026-10-27-122812-report.txt`.

Two files with the same name can land in the same subdirectory within the same
second. The plugin then adds a counter, as in
`2026-10-27-122812-1-report.txt`. No file is ever overwritten.

Because the plugin moves files rather than deleting them, nothing leaves the
watched directory on its own. Set up your own cleanup job for the three
subdirectories.

While a file is being retried it stays in the watched directory. A new write
event for that file restarts the settling timer and cancels the retries, so the
count starts again from zero.

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

### Where the config lives

Maubot does not read the config from a file you place on disk. Instead:

- Upload the plugin. Maubot reads `base-config.yaml` from the plugin archive
  and uses it as the default template.
- Create an instance in the Maubot web UI. Maubot copies the template into
  the new instance's config.
- Edit the config in the instance editor in the web UI. Maubot stores the
  result in its own database, not in a file.

`base-config.yaml` in this repository is only the template. Edit the instance
config in the web UI, not the template, so your changes apply to that instance
and survive plugin updates. Do not look for a per-instance YAML file: Maubot
keeps instance config in its database (`instance.config`), so there is nothing
to edit on disk.

The `mbc` CLI can list and edit instances, but the web UI editor is the normal
way to change this config.

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
