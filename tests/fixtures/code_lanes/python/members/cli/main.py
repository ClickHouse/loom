"""Members fixture: a click group and an argparse subcommand."""
import argparse

import click

from ..models import Store


@click.group()
def cli() -> None:
    pass


@cli.command("sync")
@click.option("--path")
def sync_cmd(path: str) -> None:
    Store(path).add(None)


@cli.command()
def dump_all() -> None:
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    sub.add_parser("ingest")
    return parser
