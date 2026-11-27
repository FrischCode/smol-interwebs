"""The pinned binary's fetch, over a fake downloader and a real zip in memory.

The downloader is the only seam: it writes whatever bytes the test hands it, so the
hash pass is exercised for real (a correct sha lands `ok`, a wrong one lands `failed`
with the part gone). The zip is built in memory with the same layout the mirror ships
(flat: the binary and its DLLs at the archive root).
"""
from __future__ import annotations

import hashlib
import io
import json
import pathlib
import struct
import zipfile

from siw import tools


def _zip_bytes(payload: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _pe(machine: int) -> bytes:
    """A minimal PE: the MZ stub with the PE header at 0x40 and the requested machine type."""
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    dos[0x3C:0x40] = struct.pack("<I", 64)
    return bytes(dos) + b"PE\x00\x00" + struct.pack("<H", machine) + b"\x00" * 16


X64 = _pe(0x8664)
I386 = _pe(0x14C)


def _pin_for(zip_bytes: bytes) -> None:
    """Point the pin at these exact bytes: the tests own the pin for their own zip."""
    tools.RELEASE["sha256"] = hashlib.sha256(zip_bytes).hexdigest()
    tools.RELEASE["size"] = len(zip_bytes)


def _restore_pin() -> None:
    tools.RELEASE["sha256"] = "fcd01ed2b93e9a68632c7863c83b9f66bf64406a66357be1df7b8b75596f3e45"
    tools.RELEASE["size"] = 18_301_924


def _downloader_serving(data: bytes, calls: list):
    def download(url: str, dest: pathlib.Path, progress):
        calls.append(url)
        dest.write_bytes(data)
        progress(len(data), len(data))
    return download


def test_missing_without_a_directory(tmp_path):
    f = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools")
    snap = f.snapshot()
    assert snap["state"] == "missing" and snap["path"] is None and snap["exists"] is False
    assert snap["progress"] is None and snap["error"] is None


def test_fetch_verifies_and_extracts(tmp_path):
    _zip = _zip_bytes({tools.RELEASE["binary"]: X64, "icuuc74.dll": b"dll"})
    _pin_for(_zip)
    try:
        calls: list = []
        f = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools",
                               downloader=_downloader_serving(_zip, calls))
        ready: list = []
        f.set_on_ready(lambda: ready.append(1))
        f.start()
        f._thread.join()
        snap = f.snapshot()
        assert snap["state"] == "ok" and snap["error"] is None
        assert snap["path"] == str(tmp_path / "tools" / "kiwix-tools" / tools.RELEASE["binary"])
        assert pathlib.Path(snap["path"]).read_bytes() == X64
        assert (tmp_path / "tools" / "kiwix-tools" / "icuuc74.dll").is_file()
        # The part is gone, the marker is the record, and the hand-off fired once.
        assert not (tmp_path / "tools" / f"kiwix-tools-{tools.RELEASE['version']}.zip.part").exists()
        mark = json.loads((tmp_path / "tools" / "kiwix-tools" / ".siw-verified").read_text())
        assert mark["sha256"] == tools.RELEASE["sha256"]
        assert ready == [1]
        assert calls == [tools.RELEASE["url"]]
    finally:
        _restore_pin()


def test_a_failed_hash_deletes_the_part_and_fails(tmp_path):
    good = _zip_bytes({tools.RELEASE["binary"]: X64})
    _zip = _zip_bytes({tools.RELEASE["binary"]: b"tampered-bytes"})
    _pin_for(good)  # the pin wants the good bytes; the mirror serves the tampered ones
    try:
        calls: list = []
        f = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools",
                               downloader=_downloader_serving(_zip, calls))
        f.start()
        f._thread.join()
        snap = f.snapshot()
        assert snap["state"] == "failed"
        assert "mismatch" in snap["error"]
        # The untrusted archive is not kept, and no binary exists to be spawned.
        assert not (tmp_path / "tools" / f"kiwix-tools-{tools.RELEASE['version']}.zip.part").exists()
        assert not (tmp_path / "tools" / "kiwix-tools" / tools.RELEASE["binary"]).exists()
    finally:
        _restore_pin()


def test_a_verified_install_is_ok_without_the_net(tmp_path):
    _zip = _zip_bytes({tools.RELEASE["binary"]: X64})
    _pin_for(_zip)
    try:
        calls: list = []
        d = tmp_path / "tools" / "kiwix-tools"
        f1 = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools",
                                downloader=_downloader_serving(_zip, calls))
        f1.start()
        f1._thread.join()
        # A new process on the same directory: verified, no download, no thread.
        f2 = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools",
                                downloader=_downloader_serving(_zip, calls))
        snap = f2.snapshot()
        assert snap["state"] == "ok" and calls == [tools.RELEASE["url"]]
        assert f2._thread is None
        f2.start()  # idempotent over a verified install
        assert calls == [tools.RELEASE["url"]]
    finally:
        _restore_pin()


def test_failed_fetch_retries_on_start(tmp_path):
    _zip = _zip_bytes({tools.RELEASE["binary"]: X64})
    _pin_for(_zip)
    try:
        calls: list = []
        f = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools",
                               downloader=_downloader_serving(_zip, calls))
        f._state = "failed"
        f.start()
        f._thread.join()
        assert f.snapshot()["state"] == "ok" and len(calls) == 1
    finally:
        _restore_pin()


def test_a_32_bit_binary_fails_the_fetch(tmp_path):
    # The pin is x86_64 on purpose (the 32-bit build's address-space ceiling). The archive
    # can be byte-perfect -- right hash, right layout -- and still be the wrong engine:
    # the PE machine type is checked, and a 32-bit binary is a failed fetch.
    _zip = _zip_bytes({tools.RELEASE["binary"]: I386, "icuuc74.dll": b"dll"})
    _pin_for(_zip)  # the hash wants exactly this (32-bit) zip: the hash passes, the PE check must not
    try:
        calls: list = []
        f = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools",
                               downloader=_downloader_serving(_zip, calls))
        f.start()
        f._thread.join()
        snap = f.snapshot()
        assert snap["state"] == "failed"
        assert "32-bit" in snap["error"] or "0x014c" in snap["error"]
        assert not (tmp_path / "tools" / "kiwix-tools" / tools.RELEASE["binary"]).exists()
    finally:
        _restore_pin()


def test_partial_delivery_fails_the_hash(tmp_path):
    _zip = _zip_bytes({tools.RELEASE["binary"]: X64})
    _pin_for(_zip)
    try:
        partial = _zip[: len(_zip) // 2]

        def partial_download(url, dest, progress):
            dest.write_bytes(partial)
            progress(len(partial), len(_zip))

        f = tools.ToolsFetcher(tmp_path / "tools" / "kiwix-tools", downloader=partial_download)
        f.start()
        f._thread.join()
        # a short read is a hash failure, not an `ok` over fewer bytes
        assert f.snapshot()["state"] == "failed"
    finally:
        _restore_pin()
