"""Terminal-only colors, safe text, and display-width-aware hanging indentation."""

import os
import shutil
import unicodedata
from typing import TextIO

from wcwidth import wrap

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = (f"\x1b[{i}m" for i in range(31, 38))
LEVEL_STYLE = {"ok": GREEN, "unknown": CYAN, "not_applicable": DIM, "info": CYAN, "warning": YELLOW, "error": RED}
LEVEL_MARKER = {"ok": "·", "unknown": "?", "not_applicable": "-", "info": "i", "warning": "!", "error": "x"}
KIND_STYLE = {
    "function": ("F", MAGENTA),
    "method": ("M", WHITE),
    "closure": ("L", MAGENTA),
    "class": ("C", CYAN),
    "struct": ("S", CYAN),
    "enum": ("E", CYAN),
    "trait": ("T", CYAN),
    "impl": ("I", CYAN),
    "interface": ("I", CYAN),
    "type": ("T", CYAN),
    "module": ("M", BLUE),
    "package": ("P", BLUE),
}
_BIDI_CONTROLS = frozenset({"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"})


def safe_text(text: str) -> str:
    """Do not let names, messages, or provider text inject terminal control sequences."""
    return "".join(
        f"\\u{ord(char):04x}"
        if (ord(char) < 32 and char != "\n")
        or 127 <= ord(char) < 160
        or unicodedata.bidirectional(char) in _BIDI_CONTROLS
        else char
        for char in text
    )


class Terminal:
    def __init__(self, stream: TextIO, width: int | None = None) -> None:
        self.stream = stream
        self.width = max(12, width or shutil.get_terminal_size(fallback=(100, 24)).columns)
        setting = os.getenv("COLOR", "auto").lower()
        self.color = os.getenv("NO_COLOR") is None and (setting == "yes" or setting == "auto" and stream.isatty())

    def paint(self, text: str, style: str) -> str:
        return f"{style}{text}{RESET}" if self.color else text

    def _write_styled(self, text: str, indent: int, continuation: int | None = None) -> None:
        prefix = " " * min(indent, self.width // 3)
        following = " " * min(indent if continuation is None else continuation, self.width // 3)
        for paragraph in text.split("\n"):
            lines = wrap(
                paragraph, self.width, initial_indent=prefix, subsequent_indent=following, break_on_hyphens=False
            )
            for line in lines or [prefix]:
                self.stream.write(line + "\n")

    def write(self, text: str, indent: int = 0, style: str = "") -> None:
        self._write_styled(self.paint(safe_text(text), style) if style else safe_text(text), indent)

    def row(self, marker: str, name: str, value: str, style: str) -> None:
        text = self.paint(marker, BOLD + style) + " " + safe_text(name) + "  " + self.paint(safe_text(value), style)
        self._write_styled(text, 8, 10)

    def header(self, marker: str, location: str, name: str, cached: bool, style: str) -> None:
        text = self.paint(marker, BOLD + style) + " " + self.paint(safe_text(location), DIM)
        text += " " + self.paint(safe_text(name), BOLD)
        if cached:
            text += self.paint("  cached", DIM)
        self._write_styled(text, 4, 6)
