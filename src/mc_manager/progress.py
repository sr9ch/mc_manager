"""Small terminal progress indicators without runtime dependencies."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Thread


@contextmanager
def activity(label: str) -> Iterator[None]:
    """Show a spinner while an operation has no measurable total."""
    stream = sys.stderr
    if not stream.isatty():
        yield
        return
    stopped = Event()

    def animate() -> None:
        index = 0
        frames = "|/-\\"
        while not stopped.is_set():
            stream.write(f"\r{label} {frames[index % len(frames)]}")
            stream.flush()
            index += 1
            stopped.wait(0.12)
        stream.write("\r" + " " * (len(label) + 3) + "\r")
        stream.flush()

    thread = Thread(target=animate, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


class TransferProgress:
    """Display byte and file progress during a repository transaction."""

    def __init__(self) -> None:
        self.stream = sys.stderr
        self.terminal = self.stream.isatty()
        self.previous = ""

    def __call__(self, phase: str, current: int, total: int) -> None:
        if not self.terminal:
            return
        fraction = min(1.0, current / max(total, 1))
        filled = int(fraction * 24)
        bar = "#" * filled + "." * (24 - filled)
        unit = "bytes" if phase == "Loading files" else "files"
        line = f"{phase} [{bar}] {fraction:3.0%} ({current}/{total} {unit})"
        if line == self.previous:
            return
        self.stream.write("\r" + line)
        if current >= total:
            self.stream.write("\n")
        self.stream.flush()
        self.previous = line
