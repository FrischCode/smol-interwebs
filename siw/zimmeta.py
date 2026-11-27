"""siw.zimmeta -- what a ZIM file says about itself, from the first 80 bytes.

The identity ladder's second rung reads the file, not the network: the header carries the book's
own UUID (the same value the kiwix catalog exposes as the entry's id) and its format version.
Everything else a ZIM carries (title, description, language) lives deeper in the container and
is not read here — a reader for the 2026 build's tail metadata block is an open item, and the console degrades to the stem plus a low-confidence ident
rather than guessing.

Measured on kiwix-tools 3.8.1-era builds, `ZIM\x04` magic, 80-byte header):

    offset 0    magic `ZIM\x04`
    offset 4    u16 version (6 in the 2026 builds)
    offset 6    u16 compression algorithm
    offset 8    16-byte book UUID -- byte-equal to the catalog entry id (Z2)
    offset 24   u32 directory entry count
    offset 28   u32 cluster count
    offset 32   u64 (role not pinned down)
    offset 48   u64 (role not pinned down)
    offset 56   u64 header size -- 80 in every build measured, and the header region ends
                exactly where the first content begins

Only the fields the product needs are returned; a header that disagrees with itself (bad magic,
wrong size field, short file) is `None`, never a partial guess."""
from __future__ import annotations

import struct
import uuid
from pathlib import Path

MAGIC = b"ZIM\x04"
HEADER_SIZE = 80


def read_header(path: str | Path) -> dict | None:
    """The 80-byte header as facts, or `None` when the file does not say what it is.

    Reading is bounded to the header on purpose: a 118 GB book is not worth a 118 GB open for a
    question the first 80 bytes answer, and a torn download (a few KB of a multi-GB file) must
    read as "not a ZIM yet", not crash the scan."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEADER_SIZE)
    except OSError:
        return None
    if len(head) < HEADER_SIZE or head[:4] != MAGIC:
        return None
    version, compression = struct.unpack_from("<HH", head, 4)
    size_field = struct.unpack_from("<Q", head, 56)[0]
    if size_field != HEADER_SIZE:
        return None
    raw_uuid = head[8:24]
    if not any(raw_uuid):
        return None
    return {
        "magic": "zim4",
        "version": int(version),
        "compression": int(compression),
        "uuid": str(uuid.UUID(bytes=raw_uuid)),
        "entries": int(struct.unpack_from("<I", head, 24)[0]),
        "clusters": int(struct.unpack_from("<I", head, 28)[0]),
    }


def book_uuid(path: str | Path) -> str:
    """The book's own id as the console prints it -- or the empty string when the file cannot say.

    The empty string (not an exception) is the ladder's "this rung did not answer": the caller
    keeps the stem and marks the ident low-confidence instead of failing the row."""
    head = read_header(path)
    return head["uuid"] if head else ""
