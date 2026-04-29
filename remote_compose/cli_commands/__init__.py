"""rc CLI command modules.

Each submodule defines a slice of the CLI surface (one or more commands /
groups). cli.py imports the commands and registers them on the root
`cli` group via `cli.add_command(...)`.

Modules use plain `@click.command()` / `@click.group()` (not
`@cli.command()`) to avoid a circular import with cli.py.
"""
