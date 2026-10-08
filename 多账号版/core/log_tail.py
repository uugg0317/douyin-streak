"""Read a bounded tail of a log without loading the complete file."""
from pathlib import Path


def read_tail(path: Path, lines: int = 300, max_bytes: int = 256 * 1024) -> str:
    lines = max(10, min(int(lines), 600))
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            offset = max(0, size - max_bytes)
            stream.seek(offset)
            data = stream.read(max_bytes)
        if offset:
            # The first bytes can be part of a UTF-8 character or partial line.
            data = data.partition(b"\n")[2]
        return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
