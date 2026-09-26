"""CLI entry point. All scan behavior is delegated to focused composition modules."""

import sys
from pathlib import Path
from typing import Any

from jevscan.cli.args import parser
from jevscan.cli.scan_command import execute_scan, prepare_scan
from jevscan.cli.scan_options import apply_overrides, list_rules
from jevscan.core.config import (
    ConfigError,
    initial_yaml,
    load_config,
    resolved_yaml,
    validate_config_path,
)


def _initialize_config(path: Path) -> int:
    validate_config_path(path)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(initial_yaml())
    print(f"Created {path}")
    return 0


def _paths(args: Any) -> list[Path]:
    return [path.absolute() for path in (args.paths or [Path.cwd()])]


def _dispatch(args: Any) -> int:
    if args.init_config:
        return _initialize_config(args.init_config)

    paths = _paths(args)
    loaded = apply_overrides(load_config(paths, args.config), args)
    if args.show_config:
        sys.stdout.write(resolved_yaml(loaded.config))
        return 0
    if args.list_rules:
        list_rules(loaded.config)
        return 0
    return execute_scan(prepare_scan(paths, loaded, args), args)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (ConfigError, OSError, ValueError) as exc:
        print(f"jevscan: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
