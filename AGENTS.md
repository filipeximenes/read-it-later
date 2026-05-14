# Agent Guidelines for read-it-later

## Protecting Production Data

The `ril` tool stores real article data in a user-configured folder (typically `~/ReadItLater`).
When running any `ril` command during development or testing, you MUST set the
`RIL_DATA_FOLDER` environment variable to a temporary directory:

```bash
RIL_DATA_FOLDER=/tmp/ril-dev ril <command>
```

This overrides the real config and keeps all test operations isolated.
Never run `ril` commands without this prefix unless the user explicitly asks
you to operate on their real data.

## Installing latest version

After making code changes, reinstall the tool with:

```bash
make install
```
