"""The acquire battery: the download-side seams over fakes and over real bytes.

The sidecar test uses the real byte shape of a `.meta4` (the metalink namespace is part of the
shape: a parser that ignores it reads nothing).
The queue tests drive the state machine with `step()` instead of sleeping on the worker's tick.
"""
from __future__ import annotations

import pathlib

import pytest

from siw import acquire

# A real sidecar's shape, trimmed to the fields the reader wants (same default namespace).
NAMESPACED_META4 = b'''<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
  <generator>MirrorBrain/2.19.0</generator>
  <file name="wikipedia_en_all_mini_2026-09.zim">
    <size>14386799064</size>
    <hash type="md5">263df3dc29731c844912a25b8953</hash>
    <hash type="sha-256">350d8956ee3d7abe2839c464c3a983f93d6934a16c7f04b10715b49439dfd349</hash>
  </file>
</metalink>
'''

# The same facts with no namespace at all: a reader that assumes a namespace would fail here.
BARE_META4 = b'''<?xml version="1.0"?>
<metalink>
  <file name="wikipedia_en_all_mini_2026-09.zim">
    <size>14386799064</size>
    <hash type="sha-256">350d8956ee3d7abe2839c464c3a983f93d6934a16c7f04b10715b49439dfd349</hash>
  </file>
</metalink>
'''


def test_parse_meta4_namespaced():
    facts = acquire.parse_meta4(NAMESPACED_META4)
    assert facts["name"] == "wikipedia_en_all_mini_2026-09.zim"
    assert facts["size"] == 14386799064
    assert facts["sha256"] == "350d8956ee3d7abe2839c464c3a983f93d6934a16c7f04b10715b49439dfd349"
    assert facts["mirrors"] == []


def test_parse_meta4_bare():
    facts = acquire.parse_meta4(BARE_META4)
    assert facts["name"] == "wikipedia_en_all_mini_2026-09.zim"
    assert facts["sha256"] == "350d8956ee3d7abe2839c464c3a983f93d6934a16c7f04b10715b49439dfd349"


def test_parse_meta4_without_a_file_refuses():
    with pytest.raises(acquire.AcquireError) as exc:
        acquire.parse_meta4(b"<metalink></metalink>")
    assert exc.value.code == "meta4_unreadable"


def test_parse_meta4_not_xml_refuses():
    with pytest.raises(acquire.AcquireError) as exc:
        acquire.parse_meta4(b"this is not xml")
    assert exc.value.code == "meta4_unreadable"


def test_queue_state_machine_over_fakes(tmp_path):
    """The whole transfer machine, driven by `step()` with a child that finishes at once:
    queued -> downloading -> (hash of the part) -> promoted to the .zim, and a part that does
    not match its checksum fails the transfer and is dropped -- it is untrusted."""
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()

    def fake_spawn(url, part: pathlib.Path):
        # The child "downloads" by writing the final bytes straight to the part.
        part.write_bytes(b"Z" * 64)

        class Child:
            returncode = 0

            def poll(self):
                return 0

            def kill(self):
                pass

        return Child()

    def fake_meta4(url):
        import hashlib
        return {"size": 64, "sha256": hashlib.sha256(b"Z" * 64).hexdigest(), "mirrors": []}

    acq = acquire.Acquirer(corpus_dir, fetch_meta4=fake_meta4, spawn=fake_spawn, run_worker=False)
    acq.queue("row-a", "https://x/row-a.zim", "row-a.zim")
    assert acq.state_of("row-a") == "queued"
    acq.step()  # promote and spawn: the part exists now
    assert acq.state_of("row-a") == "downloading"
    assert (corpus_dir / "row-a.zim.part").exists()
    acq.step()  # the child is done: verify the hash, promote the part
    assert acq.state_of("row-a") is None  # success leaves the queue: the .zim is corpus truth
    assert (corpus_dir / "row-a.zim").exists() and not (corpus_dir / "row-a.zim.part").exists()


def test_queue_checksum_mismatch_fails_and_drops_the_part(tmp_path):
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()

    def fake_spawn(url, part: pathlib.Path):
        part.write_bytes(b"wrong bytes")

        class Child:
            returncode = 0

            def poll(self):
                return 0

            def kill(self):
                pass

        return Child()

    import hashlib
    acq = acquire.Acquirer(corpus_dir,
                           fetch_meta4=lambda url: {"size": 11, "sha256": "0" * 64,
                                                    "mirrors": []},
                           spawn=fake_spawn, run_worker=False)
    acq.queue("row-b", "https://x/row-b.zim", "row-b.zim")
    acq.step()
    acq.step()
    assert acq.state_of("row-b") == "failed"
    error = acq.error("row-b")
    assert error["code"] == "checksum_mismatch"
    assert error["partial_removed"] is True  # the part is untrusted, so it is deleted, not resumed
    assert not (corpus_dir / "row-b.zim.part").exists()
    assert not (corpus_dir / "row-b.zim").exists()


# -- P1 regressions: verification on the promotion path (B3), the child's exit
# code (B3/M7), and the caller-supplied checksum reaching the transfer (B4).

def zim_header_bytes() -> bytes:
    """A minimal ZIM v2 header: magic, version, compression, a UUID, counts, and
    the size field the reader insists on (80)."""
    import struct
    import uuid as uuid_mod
    head = bytearray(80)
    head[0:4] = b"ZIM\x04"
    struct.pack_into("<HH", head, 4, 6, 4)
    head[8:24] = uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, "siw-test-book").bytes
    struct.pack_into("<II", head, 24, 1, 1)
    struct.pack_into("<Q", head, 56, 80)
    return bytes(head)


class _Child:
    """A finished child with the exit code under test."""

    def __init__(self, rc):
        self._rc = rc

    def poll(self):
        return self._rc

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return self._rc

    def kill(self):
        pass


def _spawn_writing(payload: bytes, rc=0):
    def fake_spawn(url, part):
        part.write_bytes(payload)
        return _Child(rc)
    return fake_spawn


def test_child_exit_code_fails_the_transfer_and_keeps_the_part(tmp_path):
    """A child that died mid-transfer is not a finished one: the failure names the
    exit code, and the resumable .part survives (it used to be hashed, blamed on
    the checksum, and deleted)."""
    import hashlib  # noqa: F401  (the sidecar below claims its own hash)
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()
    acq = acquire.Acquirer(corpus_dir,
                           fetch_meta4=lambda url: {"size": 999, "sha256": "a" * 64,
                                                    "mirrors": []},
                           spawn=_spawn_writing(b"half a book", rc=3), run_worker=False)
    acq.queue("row-d", "https://x/row-d.zim", "row-d.zim")
    acq.step()
    acq.step()
    assert acq.state_of("row-d") == "failed"
    error = acq.error("row-d")
    assert error["code"] == "transfer_failed"
    assert "exit code 3" in error["message"]
    assert error["partial_removed"] is False
    assert (corpus_dir / "row-d.zim.part").exists()
    assert not (corpus_dir / "row-d.zim").exists()


def test_hashless_transfer_needs_a_zim_header_to_promote(tmp_path):
    """No checksum means no promotion unless the part reads as a ZIM: garbage ends
    failed with a reason, not silently serving whatever arrived."""
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()
    acq = acquire.Acquirer(corpus_dir,
                           fetch_meta4=lambda url: {"size": 5, "sha256": None, "mirrors": []},
                           spawn=_spawn_writing(b"not a zim"), run_worker=False)
    acq.queue("row-h", "https://x/row-h.zim", "row-h.zim")
    acq.step()
    acq.step()
    assert acq.state_of("row-h") == "failed"
    assert acq.error("row-h")["code"] == "header_unreadable"
    assert (corpus_dir / "row-h.zim.part").exists()


def test_hashless_transfer_with_a_zim_header_promotes_as_header_verified(tmp_path):
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()
    acq = acquire.Acquirer(corpus_dir,
                           fetch_meta4=lambda url: {"size": 81, "sha256": None, "mirrors": []},
                           spawn=_spawn_writing(zim_header_bytes() + b"\n"), run_worker=False)
    acq.queue("row-h2", "https://x/row-h2.zim", "row-h2.zim")
    acq.step()
    acq.step()
    assert acq.state_of("row-h2") is None
    assert (corpus_dir / "row-h2.zim").exists()
    assert acq.verified_for("row-h2") == "header"


def test_hash_match_records_how_the_download_verified(tmp_path):
    """`verified_for` is what the console row and the done-notice print: the hash
    path earns "sha256"."""
    import hashlib
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()
    acq = acquire.Acquirer(corpus_dir,
                           fetch_meta4=lambda url: {"size": 3, "sha256":
                                                    hashlib.sha256(b"abc").hexdigest(),
                                                    "mirrors": []},
                           spawn=_spawn_writing(b"abc"), run_worker=False)
    acq.queue("row-v", "https://x/row-v.zim", "row-v.zim")
    acq.step()
    acq.step()
    assert (corpus_dir / "row-v.zim").exists()
    assert acq.verified_for("row-v") == "sha256"


def test_caller_sha256_rides_the_transfer_and_skips_the_sidecar(tmp_path):
    """B4: the sha the caller brought is the checksum of record -- no `.meta4` fetch,
    and the transfer compares against exactly that hash."""
    import hashlib
    corpus_dir = tmp_path / "zims"
    corpus_dir.mkdir()

    def boom(url):
        raise AssertionError("a transfer with a checksum must not fetch the sidecar")

    acq = acquire.Acquirer(corpus_dir, fetch_meta4=boom,
                           spawn=_spawn_writing(b"caller bytes"), run_worker=False)
    acq.queue("row-s", "https://x/row-s.zim", "row-s.zim",
              sha256=hashlib.sha256(b"caller bytes").hexdigest())
    acq.step()
    acq.step()
    assert (corpus_dir / "row-s.zim").exists()
    assert acq.verified_for("row-s") == "sha256"


def test_percent_reads_100_while_verifying(tmp_path):
    """n1: the cap at 99 is for bytes still arriving; during the hash every byte has
    arrived, and a 30 GB verification no longer reads as a download stuck at 99."""
    acq = acquire.Acquirer(tmp_path, run_worker=False)
    acq.queue("r", "https://x.example/r.zim", "r.zim")
    t = acq._transfers["r"]
    (tmp_path / "r.zim.part").write_bytes(b"0" * 100)
    t.state = acquire.DOWNLOADING
    t.size = 100
    assert t.progress(1)["percent"] == 99  # capped while the transfer is the act
    t.state = acquire.VERIFYING
    assert t.progress(1)["percent"] == 100  # and honest once only the hash remains


def test_the_wget_fallback_never_combines_continue_with_output(monkeypatch):
    """m1: wget's own manual is explicit that -c with -O does not resume correctly;
    resume belongs to the curl path, and the fallback starts fresh."""
    monkeypatch.setattr(acquire.shutil, "which",
                        lambda name: None if name == "curl" else "/usr/bin/wget")
    tool, argv = acquire._download_tool()
    assert tool == "wget" and "-c" not in argv and "-O" in argv
