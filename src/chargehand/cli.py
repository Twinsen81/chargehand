import argparse
from collections.abc import Sequence

from chargehand import __version__

DESCRIPTION = (
    "Label an issue, get a supervised Claude Code session in a fresh git worktree. "
    "Pre-alpha: no commands are implemented yet; see docs/DESIGN.md."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chargehand", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"chargehand {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
