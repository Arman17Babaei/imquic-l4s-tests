#!/usr/bin/env python3
"""Validate and render strict IMQUIC Prague experiment profiles.

The accepted file is a deliberately small YAML subset: nested mappings,
two-space indentation, scalar strings, and integers. This keeps the helper
standard-library-only and avoids adding a YAML runtime dependency to IMQUIC.
"""

from __future__ import annotations

import argparse
import re
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

PARAMETERS = (
    "alpha_gain",
    "ce_response",
    "loss_beta",
    "sudden_ce_threshold",
)
_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")
_FRACTION = re.compile(r"^(0|[1-9][0-9]*)/(0|[1-9][0-9]*)$")


class ProfileError(ValueError):
    """Raised when a profile file violates the strict schema."""


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _scalar(value: str, line_number: int) -> Any:
    value = value.strip()
    if not value:
        raise ProfileError(f"line {line_number}: missing scalar value")
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    return value


def parse_strict_yaml(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-2, root)]

    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if "\t" in line:
            raise ProfileError(f"line {line_number}: tabs are not allowed")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise ProfileError(f"line {line_number}: indentation must use two spaces")
        content = line.strip()
        if ":" not in content:
            raise ProfileError(f"line {line_number}: expected key: value")
        key, value = content.split(":", 1)
        key = key.strip()
        if not _KEY.fullmatch(key):
            raise ProfileError(f"line {line_number}: invalid key {key!r}")

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack or indent != stack[-1][0] + 2:
            raise ProfileError(f"line {line_number}: invalid indentation depth")
        parent = stack[-1][1]
        if key in parent:
            raise ProfileError(f"line {line_number}: duplicate key {key}")

        if value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(value, line_number)

    return root


def _fraction(name: str, raw: Any) -> Fraction:
    if not isinstance(raw, str) or not _FRACTION.fullmatch(raw):
        raise ProfileError(f"{name} must be a rational value such as 1/16")
    numerator, denominator = (int(part) for part in raw.split("/", 1))
    if denominator == 0:
        raise ProfileError(f"{name} denominator must not be zero")
    value = Fraction(numerator, denominator)
    if value <= 0 or value > 1:
        raise ProfileError(f"{name} must be greater than 0 and at most 1")
    return value


def load_profile(path: Path, requested: str | None = None) -> tuple[str, dict[str, str]]:
    try:
        document = parse_strict_yaml(path)
    except OSError as exc:
        raise ProfileError(str(exc)) from exc

    if document.get("version") != 1:
        raise ProfileError("version must be 1")
    default = document.get("default_profile")
    profiles = document.get("profiles")
    if not isinstance(default, str) or not default:
        raise ProfileError("default_profile must name a profile")
    if not isinstance(profiles, dict) or not profiles:
        raise ProfileError("profiles must be a non-empty mapping")

    name = requested or default
    if name not in profiles:
        raise ProfileError(f"unknown profile: {name}")
    profile = profiles[name]
    if not isinstance(profile, dict):
        raise ProfileError(f"profile {name} must be a mapping")

    missing = [parameter for parameter in PARAMETERS if parameter not in profile]
    extra = [key for key in profile if key not in PARAMETERS]
    if missing:
        raise ProfileError(f"profile {name} is missing: {', '.join(missing)}")
    if extra:
        raise ProfileError(f"profile {name} has unknown fields: {', '.join(sorted(extra))}")

    normalized: dict[str, str] = {}
    for parameter in PARAMETERS:
        value = _fraction(parameter, profile[parameter])
        normalized[parameter] = f"{value.numerator}/{value.denominator}"
    return name, normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "render-options"):
        sub = subparsers.add_parser(command)
        sub.add_argument("file", type=Path)
        sub.add_argument("profile", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        name, profile = load_profile(args.file, args.profile)
    except ProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "validate":
        print(f"valid profile: {name}")
    else:
        print(",".join(f"{key}={profile[key]}" for key in PARAMETERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
