"""Write-back cache single-disk simulator.

The "disk" is a local image file addressed in whole 4 KiB blocks. Normal
writes complete from an in-memory cache; FLUSH persists a selected set of
dirty blocks over exactly one round (which can be paused), FUA writes force
the specified block to the platter before returning, and power loss drops the
cache and aborts any in-flight FLUSH, after which reads return the contents
of the original image again.

Torn-write safety
-----------------
A platter write can fail halfway (a *short write*): the first N bytes of the
block reach the image and the rest do not.  The FLUSH round then ends at
once: blocks already persisted keep their platter values, and the failed and
unprocessed blocks keep their newest cache values for a later retry.

To keep the image's previous complete value intact, every block write-back
first records the old 4 KiB block in a small sidecar undo file
(``<image>.undo``) and fsyncs it before touching the image.  After a failed
write only the N bytes that were actually overwritten are rolled back, so the
untouched suffix (still the old value) needs no write at all - the rollback
works even if the tail of that block stays unwritable.  If the rollback write
itself fails, the journal stays active: reads fall back to it, and the next
power loss / process start replays it.

The journal holds at most one block (a FLUSH round stops at the first
failure), so the extra I/O per block write-back is a constant number of 4 KiB
reads/writes and does not grow with the image capacity.

Concurrency model: a single reentrant lock guards all mutable state. The
FLUSH worker persists blocks one by one and waits on a pause event between
blocks; each persistence re-checks block versions so stale snapshots can
never overwrite a newer on-platter value.
"""

from __future__ import annotations

import collections
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field

BLOCK_SIZE = 4096
DEFAULT_BLOCK_COUNT = 32

# FLUSH round lifecycle
FLUSH_IDLE = "idle"
FLUSH_RUNNING = "running"
FLUSH_PAUSED = "paused"
FLUSH_DONE = "done"
FLUSH_ABORTED = "aborted"
FLUSH_FAILED = "failed"

# Sidecar undo journal: one fixed-size record (header + one 4 KiB block).
# `written` is how many leading bytes of the attempted new value reached the
# image; only that prefix needs to be rolled back, the untouched suffix still
# holds the old value.
UNDO_MAGIC = b"WBCACHE-UNDO\x00\x01\x00\x00"
UNDO_HEADER = struct.Struct(">16sIIIII")  # magic, block, seq, written, crc32, length
UNDO_SUFFIX = ".undo"


class DiskError(Exception):
    """Base error for invalid simulator operations."""


class BlockError(DiskError):
    """Invalid block index or payload."""


class FlushBusyError(DiskError):
    """A FLUSH round is already active."""


class BlockWriteError(DiskError):
    """A block write-back did not reach the platter completely."""

    def __init__(
        self,
        block: int,
        written: int,
        expected: int,
        detail: str,
        uncertain: bool = False,
    ):
        super().__init__(detail)
        self.block = block
        self.written = written
        self.expected = expected
        # True when an OS-level failure gave no byte count: assume any part of
        # the block may have changed and restore the whole old value.
        self.uncertain = uncertain


class ShortWriteError(BlockWriteError):
    """Only a prefix of the block was written before the write failed."""


@dataclass
class CacheEntry:
    data: bytes
    seq: int  # write sequence number assigned at accept time


@dataclass
class FlushSession:
    seq: int
    selected: list[int]
    snapshots: dict[int, CacheEntry]
    cursor: int = 0  # index into `selected` of the next block to persist
    status: str = FLUSH_RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # per-block outcome once handled:
    # persisted | skipped-clean | skipped-stale | failed-short-write
    results: dict[int, str] = field(default_factory=dict)
    failed_block: int | None = None
    error: str | None = None


class WriteBackDisk:
    """A single disk with a volatile write-back cache and a local image."""

    def __init__(
        self,
        image_path: str,
        block_count: int = DEFAULT_BLOCK_COUNT,
        flush_delay: float = 0.6,
        image_initialized: bool | None = None,
    ):
        if block_count <= 0:
            raise BlockError("block_count must be positive")
        self.image_path = image_path
        self.block_count = block_count
        self.flush_delay = flush_delay

        self._lock = threading.RLock()
        self._wake = threading.Event()  # set while a FLUSH round may run
        self._wake.set()
        self._log: collections.deque[dict] = collections.deque(maxlen=200)
        self._next_seq = 1
        self._event_id = 1
        self._power_cycle_count = 0

        # block index -> newest accepted write not yet known on the platter
        self._cache: dict[int, CacheEntry] = {}
        # block index -> version currently on the platter (memory mirror of
        # what was persisted; wiped on power loss together with the cache)
        self._persisted_version: dict[int, int] = {}
        self._session: FlushSession | None = None
        self._worker: threading.Thread | None = None

        # Fault injection: block -> (prefix_bytes, persistent).  The next
        # FLUSH write-back for that block writes only the prefix and fails
        # with ShortWriteError.  Persistent faults stay armed until cleared.
        self._short_write_faults: dict[int, tuple[int, bool]] = {}
        self._last_failure: dict | None = None
        # I/O accounting for the constant-cost guarantee (image + undo file).
        self._io_read_bytes = 0
        self._io_write_bytes = 0

        os.makedirs(os.path.dirname(os.path.abspath(image_path)) or ".", exist_ok=True)
        already_exists = os.path.exists(image_path)
        if image_initialized is None:
            image_initialized = already_exists and os.path.getsize(image_path) == self.capacity
        if not image_initialized:
            self._reset_image_file()
            if already_exists:
                self._log_event("image.recreated", "existing image had wrong size, recreated")
            # A stale journal refers to the old image; it must not be replayed.
            self._clear_undo_locked()
        elif not already_exists:
            self._reset_image_file()
        else:
            # Simulated "restart": replay any undo record left by a failed
            # write-back whose rollback could not be written.
            self._recover_undo_locked("startup recovery")

    # ------------------------------------------------------------------ basics
    @property
    def capacity(self) -> int:
        return self.block_count * BLOCK_SIZE

    @property
    def undo_path(self) -> str:
        return self.image_path + UNDO_SUFFIX

    def _reset_image_file(self) -> None:
        with open(self.image_path, "wb") as fh:
            for block in range(self.block_count):
                marker = f"INIT-BLOCK-{block:02d}".encode().ljust(BLOCK_SIZE, b"\x00")
                fh.write(marker)
            fh.flush()
            os.fsync(fh.fileno())

    def _fsync_dir(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.undo_path)) or "."
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _check_block(self, block: int) -> None:
        # bool is a subclass of int: reject True/False as block indices
        if isinstance(block, bool) or not isinstance(block, int):
            raise BlockError(f"block index must be an integer, got {block!r}")
        if block < 0 or block >= self.block_count:
            raise BlockError(f"block index out of range: {block!r}")

    def _check_payload(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise BlockError("payload must be bytes")
        if len(data) != BLOCK_SIZE:
            raise BlockError(f"payload must be exactly {BLOCK_SIZE} bytes, got {len(data)}")

    def _log_event(self, kind: str, detail: str = "", **extra) -> dict:
        event = {
            "id": self._event_id,
            "ts": round(time.time(), 3),
            "kind": kind,
            "detail": detail,
        }
        event.update(extra)
        self._event_id += 1
        self._log.append(event)
        return event

    def events(self, after_id: int = 0) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._log if e["id"] > after_id]

    # ------------------------------------------------------------- block I/O
    def _read_image_block(self, block: int) -> bytes:
        """Read exactly one block from the image through a positional read."""
        fd = os.open(self.image_path, os.O_RDONLY)
        try:
            data = os.pread(fd, BLOCK_SIZE, block * BLOCK_SIZE)
        finally:
            os.close(fd)
        self._io_read_bytes += len(data)
        if len(data) != BLOCK_SIZE:
            raise DiskError("image short read; image file may be corrupted")
        return data

    def _write_image_block(
        self, block: int, data: bytes, fault_prefix: int | None = None
    ) -> None:
        """Write one block at its 4 KiB offset.

        ``fault_prefix`` injects a short write: only that many leading bytes
        reach the image, then the call raises ShortWriteError.  A real short
        write (``os.pwrite`` writing fewer bytes) is reported the same way.
        """
        if len(data) != BLOCK_SIZE:
            raise BlockError(f"payload must be exactly {BLOCK_SIZE} bytes")
        payload = data if fault_prefix is None else data[:fault_prefix]
        written = 0
        try:
            fd = os.open(self.image_path, os.O_RDWR)
        except OSError as exc:
            raise BlockWriteError(
                block, 0, BLOCK_SIZE, f"block write failed: {exc}", uncertain=True
            ) from exc
        try:
            try:
                written = os.pwrite(fd, payload, block * BLOCK_SIZE)
                self._io_write_bytes += written
                os.fsync(fd)
            except OSError as exc:
                raise BlockWriteError(
                    block,
                    written,
                    BLOCK_SIZE,
                    f"block write failed: {exc}",
                    uncertain=True,
                ) from exc
        finally:
            os.close(fd)
        if fault_prefix is not None:
            raise ShortWriteError(
                block,
                written,
                BLOCK_SIZE,
                f"injected short write: {written} of {BLOCK_SIZE} bytes reached the image",
            )
        if written != BLOCK_SIZE:
            raise ShortWriteError(
                block,
                written,
                BLOCK_SIZE,
                f"short write: {written} of {BLOCK_SIZE} bytes reached the image",
            )

    def _write_image_prefix(self, block: int, data: bytes) -> None:
        """Write ``data`` at the start of a block (used for rollback).

        Only the bytes actually overwritten by the failed attempt are
        rewritten.  The untouched suffix of the block still holds the old
        value, so a rollback works even when the tail of the block cannot be
        written at all.
        """
        if not data:
            return
        fd = os.open(self.image_path, os.O_RDWR)
        try:
            written = os.pwrite(fd, data, block * BLOCK_SIZE)
            self._io_write_bytes += written
            os.fsync(fd)
        finally:
            os.close(fd)
        if written != len(data):
            raise BlockWriteError(
                block,
                written,
                len(data),
                f"short rollback write: {written} of {len(data)} bytes restored",
            )

    # ------------------------------------------------------------ undo journal
    def _read_undo_locked(self, account: bool = True) -> tuple[int, int, int, bytes] | None:
        """Return (block, seq, written, old_data) for an active undo record."""
        try:
            with open(self.undo_path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if account:
            self._io_read_bytes += len(raw)
        if len(raw) < UNDO_HEADER.size:
            return None
        magic, block, seq, written, crc, data_len = UNDO_HEADER.unpack_from(raw)
        data = raw[UNDO_HEADER.size : UNDO_HEADER.size + data_len]
        if (
            magic != UNDO_MAGIC
            or data_len != BLOCK_SIZE
            or len(data) != BLOCK_SIZE
            or block >= self.block_count
            or written > BLOCK_SIZE
            or (zlib.crc32(data) & 0xFFFFFFFF) != crc
        ):
            return None
        return block, seq, written, data

    def _write_undo_locked(self, block: int, seq: int, written: int, old_data: bytes) -> None:
        """Durably record the old block value before changing the image."""
        header = UNDO_HEADER.pack(
            UNDO_MAGIC,
            block,
            seq,
            written,
            zlib.crc32(old_data) & 0xFFFFFFFF,
            BLOCK_SIZE,
        )
        payload = header + old_data
        tmp = f"{self.undo_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            self._io_write_bytes += len(payload)
            os.replace(tmp, self.undo_path)
            self._fsync_dir()
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _clear_undo_locked(self) -> None:
        try:
            os.unlink(self.undo_path)
        except FileNotFoundError:
            return
        self._fsync_dir()

    def _recover_undo_locked(self, reason: str) -> bool:
        """Restore the journaled original bytes into the image."""
        undo = self._read_undo_locked()
        if undo is None:
            return False
        block, seq, written, old_data = undo
        try:
            # Only the prefix the failed attempt overwrote needs restoring;
            # this succeeds even if the rest of the block stays unwritable.
            self._write_image_prefix(block, old_data[:written])
        except (BlockWriteError, OSError) as exc:
            self._log_event(
                "undo.recovery-blocked",
                f"block {block}: original prefix could not be rewritten ({exc}); "
                f"undo journal stays active and reads fall back to it",
                block=block,
                seq=seq,
            )
            return False
        try:
            self._clear_undo_locked()
        except OSError as exc:
            self._log_event(
                "undo.clear-failed",
                f"block {block} restored but undo journal could not be removed: {exc}",
                block=block,
            )
        self._log_event(
            "undo.recovered",
            f"block {block} original {written} overwritten byte(s) restored from "
            f"undo journal ({reason})",
            block=block,
            seq=seq,
            written=written,
        )
        return True

    # ------------------------------------------------------------------ writes
    def _accept_write(self, block: int, data: bytes) -> int:
        """Apply a write to the cache and return its sequence number."""
        self._check_block(block)
        self._check_payload(data)
        seq = self._next_seq
        self._next_seq += 1
        self._cache[block] = CacheEntry(bytes(data), seq)
        return seq

    def _record_failure(
        self,
        block: int,
        seq: int,
        written: int,
        detail: str,
        recovered: bool | None,
        old_data: bytes | None = None,
    ) -> None:
        self._last_failure = {
            "block": block,
            "seq": seq,
            "written": written,
            "expected": BLOCK_SIZE,
            "error": detail,
            "rolled_back": bool(recovered),
            "at": round(time.time(), 3),
        }
        if recovered is None:
            outcome = "image left untouched"
        elif recovered:
            outcome = "original 4 KiB block restored in the image"
        else:
            outcome = "image still torn, original kept in the undo journal"
        self._log_event(
            "flush.short-write" if written < BLOCK_SIZE else "flush.write-error",
            f"block {block} seq {seq}: {detail}; {outcome}",
            block=block,
            seq=seq,
            written=written,
            expected=BLOCK_SIZE,
            rolled_back=bool(recovered),
        )
        if recovered is False and old_data is not None:
            # Rollback could not be written.  Narrow the journal to the bytes
            # that really need restoring, so a later recovery only rewrites
            # that prefix and leaves the untouched tail (still the old value)
            # alone - it works even when the tail stays unwritable.
            try:
                self._write_undo_locked(block, seq, written, old_data)
            except OSError as exc:
                self._log_event(
                    "undo.update-failed",
                    f"block {block}: could not narrow undo journal ({exc}); "
                    f"recovery will restore the whole block",
                    block=block,
                )

    def _rollback_locked(self, block: int, written: int, old_data: bytes) -> bool:
        """Restore the bytes a failed write-back already overwrote.

        Only ``written`` leading bytes were touched; rewriting just that
        prefix leaves the tail (still the old value) alone, so the original
        4 KiB block is intact again even if the block cannot be fully written.
        """
        try:
            self._write_image_prefix(block, old_data[:written])
        except (BlockWriteError, OSError) as exc:
            self._log_event(
                "flush.rollback-failed",
                f"block {block}: rollback write failed ({exc}); "
                f"undo journal keeps the original value",
                block=block,
            )
            return False
        try:
            self._clear_undo_locked()
        except OSError as exc:
            self._log_event(
                "undo.clear-failed",
                f"block {block} rolled back but undo journal could not be removed: {exc}",
                block=block,
            )
        self._log_event(
            "flush.rollback",
            f"block {block}: {written} overwritten byte(s) restored to the original value",
            block=block,
        )
        return True

    def _fault_prefix(self, block: int) -> int | None:
        """Prefix length for an injected short write, if one is armed.

        A one-shot fault is consumed here; a persistent one stays armed.
        Rollback/recovery never goes through this path, so only a real FLUSH
        write-back spends a fault.
        """
        fault = self._short_write_faults.get(block)
        if fault is None:
            return None
        prefix, persistent = fault
        if not persistent:
            del self._short_write_faults[block]
        return prefix

    def _persist_locked(self, block: int, entry: CacheEntry, reason: str) -> None:
        """Write one block through to the image (caller holds the lock).

        The old value is journaled first.  On failure the image is rolled back
        to the old value; if even the rollback cannot be written the journal
        remains active for read fallback and later recovery.
        """
        undo = self._read_undo_locked()
        if undo is not None:
            if undo[0] != block:
                # At most one failure can be outstanding, but if a stale record
                # exists for another block, make the image consistent first.
                self._recover_undo_locked("before next write-back")
                undo = None
            else:
                # A previous write-back of *this* block left an unrestored
                # journal (rollback could not be written, or the process died
                # mid-write).  Repair the image first; never read a possibly
                # torn block as the "old" value.
                self._recover_undo_locked("before retrying this block")
            undo = self._read_undo_locked()

        if undo is not None:
            # Previous attempt journaled this block but never completed the
            # restore: keep the journaled original as the rollback value.
            old_data = undo[3]
        else:
            try:
                old_data = self._read_image_block(block)
                # Journal the whole old block: if the process dies during the
                # write we cannot know how many bytes landed, so recovery
                # rewrites the full original value.
                self._write_undo_locked(block, entry.seq, BLOCK_SIZE, old_data)
            except (OSError, DiskError) as exc:
                self._record_failure(
                    block, entry.seq, 0, f"could not prepare undo backup: {exc}", None
                )
                raise BlockWriteError(
                    block, 0, BLOCK_SIZE, f"could not prepare undo backup: {exc}"
                ) from exc

        fault_prefix = self._fault_prefix(block) if reason.startswith("FLUSH") else None

        try:
            self._write_image_block(block, entry.data, fault_prefix=fault_prefix)
        except (BlockWriteError, OSError) as exc:
            if isinstance(exc, BlockWriteError):
                written = BLOCK_SIZE if exc.uncertain else exc.written
            else:
                written = BLOCK_SIZE  # unknown extent: assume the worst
            recovered = self._rollback_locked(block, written, old_data)
            self._record_failure(
                block, entry.seq, written, str(exc), recovered, old_data
            )
            error_cls = type(exc) if isinstance(exc, ShortWriteError) else BlockWriteError
            raise error_cls(block, written, BLOCK_SIZE, str(exc)) from exc

        # Full block is on the platter: only now may the undo record go away.
        try:
            self._clear_undo_locked()
        except OSError as exc:
            self._log_event(
                "undo.clear-failed",
                f"block {block} persisted but undo journal could not be removed: {exc}",
                block=block,
            )

        self._persisted_version[block] = entry.seq
        cached = self._cache.get(block)
        if cached is not None and cached.seq == entry.seq:
            # nothing newer pending for this block: cache copy now redundant
            del self._cache[block]
        if self._last_failure is not None and self._last_failure.get("block") == block:
            self._last_failure = None
        self._log_event(
            "persist",
            f"block {block} seq {entry.seq} written to image ({reason})",
            block=block,
            seq=entry.seq,
            reason=reason,
        )

    def write(self, block: int, data: bytes, fua: bool = False) -> dict:
        with self._lock:
            seq = self._accept_write(block, data)
            mode = "FUA" if fua else "normal"
            self._log_event(
                "write.accept",
                f"block {block} seq {seq} {mode} write accepted into cache",
                block=block,
                seq=seq,
                fua=fua,
            )
            persisted_now = False
            if fua:
                # FUA: this block must be on the platter before we answer.
                try:
                    self._persist_locked(block, self._cache[block], "FUA")
                except BlockWriteError as exc:
                    raise DiskError(f"FUA write failed: {exc}") from exc
                persisted_now = True
            return {"block": block, "seq": seq, "fua": fua, "persisted": persisted_now}

    # ------------------------------------------------------------------- reads
    def read(self, block: int) -> dict:
        self._check_block(block)
        with self._lock:
            entry = self._cache.get(block)
            if entry is not None:
                self._log_event(
                    "read.cache",
                    f"block {block} served from cache (seq {entry.seq})",
                    block=block,
                    seq=entry.seq,
                )
                return {"block": block, "source": "cache", "seq": entry.seq, "data": entry.data}
            undo = self._read_undo_locked()
            if undo is not None and undo[0] == block:
                # Last write-back to this block failed and the image could not
                # be rolled back: the journal holds the original complete block.
                self._log_event(
                    "read.undo",
                    f"block {block} served from undo journal (original value)",
                    block=block,
                    seq=undo[1],
                )
                return {"block": block, "source": "undo", "seq": None, "data": undo[3]}
            # Read the image while still holding the lock: the FLUSH worker
            # holds the same lock across its write, so a reader can never
            # observe a half-written block.
            data = self._read_image_block(block)
            if len(data) != BLOCK_SIZE:
                raise DiskError("image short read; image file may be corrupted")
            seq = self._persisted_version.get(block)
            source = "platter" if seq is not None else "image"
            self._log_event(
                f"read.{source}",
                f"block {block} read from image" + (f" (seq {seq})" if seq else " (initial content)"),
                block=block,
                seq=seq,
            )
            return {"block": block, "source": source, "seq": seq, "data": data}

    # ------------------------------------------------------------------- flush
    def start_flush(self, selected: list[int]) -> dict:
        with self._lock:
            if self._session is not None and self._session.status in (FLUSH_RUNNING, FLUSH_PAUSED):
                raise FlushBusyError("a FLUSH round is already active")
            if any(isinstance(b, bool) or not isinstance(b, int) for b in selected):
                raise BlockError("block indices must be integers, not booleans or other types")
            blocks = sorted(set(selected))
            for b in blocks:
                self._check_block(b)

            # Snapshot exactly what is dirty *at accept time*. Writes landing
            # afterwards must not ride this round.
            snapshots: dict[int, CacheEntry] = {}
            for b in blocks:
                entry = self._cache.get(b)
                if entry is not None:
                    snapshots[b] = entry
            seq = self._next_seq
            self._next_seq += 1
            self._session = FlushSession(seq=seq, selected=blocks, snapshots=snapshots)
            self._wake.set()
            self._log_event(
                "flush.accept",
                f"FLUSH seq {seq} accepted: {len(snapshots)} dirty of "
                f"{len(blocks)} selected block(s); one round only",
                seq=seq,
                selected=blocks,
                dirty=list(snapshots.keys()),
            )
            self._worker = threading.Thread(
                target=self._flush_worker, args=(seq,), name=f"flush-{seq}", daemon=True
            )
            self._worker.start()
            return self.session_state()

    def pause_flush(self) -> dict:
        with self._lock:
            if self._session is None or self._session.status != FLUSH_RUNNING:
                raise FlushBusyError("no running FLUSH round to pause")
            self._session.status = FLUSH_PAUSED
            self._wake.clear()
            self._log_event(
                "flush.pause",
                f"FLUSH seq {self._session.seq} paused after "
                f"{self._session.cursor}/{len(self._session.selected)} block(s)",
                seq=self._session.seq,
                cursor=self._session.cursor,
            )
            return self.session_state()

    def resume_flush(self) -> dict:
        with self._lock:
            if self._session is None or self._session.status != FLUSH_PAUSED:
                raise FlushBusyError("no paused FLUSH round to resume")
            self._session.status = FLUSH_RUNNING
            self._wake.set()
            self._log_event(
                "flush.resume",
                f"FLUSH seq {self._session.seq} resumed at block cursor "
                f"{self._session.cursor}",
                seq=self._session.seq,
                cursor=self._session.cursor,
            )
            return self.session_state()

    def _finish_failed_locked(self, session: FlushSession, block: int, exc: DiskError) -> None:
        kind = (
            "failed-short-write"
            if isinstance(exc, ShortWriteError) or getattr(exc, "written", BLOCK_SIZE) < BLOCK_SIZE
            else "failed-write"
        )
        session.results[block] = kind
        session.failed_block = block
        session.error = str(exc)
        session.status = FLUSH_FAILED
        session.finished_at = time.time()
        self._log_event(
            "flush.failed",
            f"FLUSH seq {session.seq} ended at block {block}: {exc}; "
            f"{session.cursor}/{len(session.selected)} block(s) handled, "
            f"remaining dirty blocks stay in cache",
            seq=session.seq,
            block=block,
            cursor=session.cursor,
            total=len(session.selected),
            error=str(exc),
        )

    def _flush_worker(self, session_seq: int) -> None:
        while True:
            # Block while paused (or while the round has been aborted).
            if not self._wake.wait(timeout=0.25):
                with self._lock:
                    session = self._session
                    if (
                        session is None
                        or session.seq != session_seq
                        or session.status == FLUSH_ABORTED
                    ):
                        return
                continue

            with self._lock:
                session = self._session
                if (
                    session is None
                    or session.seq != session_seq
                    or session.status == FLUSH_ABORTED
                ):
                    return  # power loss / reset aborted this round
                if session.status == FLUSH_PAUSED:
                    continue
                if session.cursor >= len(session.selected):
                    session.status = FLUSH_DONE
                    session.finished_at = time.time()
                    self._log_event(
                        "flush.done",
                        f"FLUSH seq {session.seq} completed: all selected "
                        f"blocks on the platter",
                        seq=session.seq,
                        results=dict(session.results),
                    )
                    return
                block = session.selected[session.cursor]
                snapshot = session.snapshots.get(block)

                if snapshot is None:
                    # Selected block was clean at accept time; nothing from
                    # this round to persist for it.
                    session.results[block] = "skipped-clean"
                    session.cursor += 1
                    continue

                on_platter = self._persisted_version.get(block, 0)
                if snapshot.seq <= on_platter:
                    # A newer value (e.g. an FUA write during pause) already
                    # reached the platter: the old snapshot must not overwrite.
                    session.results[block] = "skipped-stale"
                    self._log_event(
                        "flush.skip-stale",
                        f"block {block}: snapshot seq {snapshot.seq} not "
                        f"overwriting newer platter seq {on_platter}",
                        seq=session.seq,
                        block=block,
                    )
                    session.cursor += 1
                    continue

                try:
                    self._persist_locked(block, snapshot, f"FLUSH seq {session_seq}")
                except BlockWriteError as exc:
                    # A short/failed write ends this round immediately. Blocks
                    # already persisted keep their platter values; the failed
                    # block and unprocessed blocks keep their cache values.
                    self._finish_failed_locked(session, block, exc)
                    return
                session.results[block] = "persisted"
                session.cursor += 1

            # Pace the round so a human can pause between blocks.
            time.sleep(self.flush_delay)

    # ------------------------------------------------------------- fault setup
    def arm_short_write(
        self, block: int, prefix_bytes: int | None = None, persistent: bool = True
    ) -> dict:
        """Arm a short write for the next FLUSH write-back of ``block``.

        ``persistent`` keeps the fault armed until explicitly cleared, so a
        retry keeps failing until the operator says the fault is gone.
        """
        with self._lock:
            self._check_block(block)
            if prefix_bytes is None:
                prefix_bytes = BLOCK_SIZE // 2
            if (
                isinstance(prefix_bytes, bool)
                or not isinstance(prefix_bytes, int)
                or prefix_bytes < 0
                or prefix_bytes >= BLOCK_SIZE
            ):
                raise BlockError(
                    f"short-write prefix must be an integer in [0, {BLOCK_SIZE}), "
                    f"got {prefix_bytes!r}"
                )
            self._short_write_faults[block] = (prefix_bytes, bool(persistent))
            self._log_event(
                "fault.arm",
                f"block {block}: next FLUSH write-back will stop after "
                f"{prefix_bytes} of {BLOCK_SIZE} bytes"
                + (" (stays armed until cleared)" if persistent else " (one-shot)"),
                block=block,
                bytes=prefix_bytes,
                persistent=bool(persistent),
            )
            return self.fault_state()

    def clear_faults(self) -> dict:
        with self._lock:
            cleared = sorted(self._short_write_faults)
            self._short_write_faults.clear()
            if cleared:
                self._log_event(
                    "fault.clear",
                    f"cleared short-write faults for block(s) {cleared}",
                    blocks=cleared,
                )
            return self.fault_state()

    def fault_state(self) -> dict:
        with self._lock:
            return {
                "short_write": {
                    str(b): {"bytes": prefix, "persistent": persistent}
                    for b, (prefix, persistent) in sorted(self._short_write_faults.items())
                }
            }

    def io_stats(self) -> dict:
        with self._lock:
            return {
                "read_bytes": self._io_read_bytes,
                "write_bytes": self._io_write_bytes,
                "total_bytes": self._io_read_bytes + self._io_write_bytes,
            }

    # ------------------------------------------------------------- power loss
    def power_loss(self) -> dict:
        with self._lock:
            aborted: FlushSession | None = None
            if self._session is not None and self._session.status in (FLUSH_RUNNING, FLUSH_PAUSED):
                self._session.status = FLUSH_ABORTED
                self._session.finished_at = time.time()
                aborted = self._session
            # keep the (now terminal) session reference so state() reports
            # "aborted"; start_flush() rejects only running/paused sessions
            self._wake.clear()  # halt the worker until the next FLUSH accept
            self._cache.clear()
            self._persisted_version.clear()
            self._power_cycle_count += 1
            # A torn block left without a successful rollback is repaired by
            # replaying the undo journal, as a restart would do.
            undo = self._read_undo_locked()
            repaired = self._recover_undo_locked("power loss recovery")
            detail = (
                f"cache dropped; FLUSH seq {aborted.seq} aborted; "
                if aborted is not None
                else "cache dropped; no active FLUSH; "
            )
            if undo is not None:
                detail += (
                    f"undo journal replayed for block {undo[0]}"
                    + ("" if repaired else " (replay failed; reads use undo copy)")
                    + "; "
                )
            detail += "reads now come from the original image again"
            self._log_event(
                "powerloss",
                detail,
                aborted_seq=aborted.seq if aborted else None,
                power_cycle=self._power_cycle_count,
                undo_block=undo[0] if undo else None,
                undo_recovered=repaired,
            )
            state = self.state()
        # Join without holding the lock so a worker finishing persistence
        # cannot deadlock against us.
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        return state

    def reset(self) -> dict:
        """Wipe cache/state and rebuild a fresh initial image (demo helper)."""
        with self._lock:
            if self._session is not None and self._session.status in (FLUSH_RUNNING, FLUSH_PAUSED):
                self._session.status = FLUSH_ABORTED
                self._session.finished_at = time.time()
            self._session = None
            self._wake.clear()
            aborted_worker = self._worker
            self._cache.clear()
            self._persisted_version.clear()
            self._short_write_faults.clear()
            self._last_failure = None
            self._next_seq = 1
            self._io_read_bytes = 0
            self._io_write_bytes = 0
            self._reset_image_file()
            self._clear_undo_locked()
            self._log_event("reset", "simulator reset: fresh initial image and empty cache")
        if aborted_worker is not None and aborted_worker.is_alive():
            aborted_worker.join(timeout=5)
        with self._lock:
            self._wake.set()
            return self.state()

    # -------------------------------------------------------------------- view
    def session_state(self) -> dict:
        with self._lock:
            session = self._session
            if session is None:
                return {"status": FLUSH_IDLE}
            return {
                "status": session.status,
                "seq": session.seq,
                "selected": list(session.selected),
                "snapshots": {str(b): e.seq for b, e in session.snapshots.items()},
                "cursor": session.cursor,
                "total": len(session.selected),
                "results": dict(session.results),
                "failed_block": session.failed_block,
                "error": session.error,
                "started_at": session.started_at,
                "finished_at": session.finished_at,
            }

    def _block_state_locked(self, block: int, undo_block: int | None) -> dict:
        cached = self._cache.get(block)
        platter_seq = self._persisted_version.get(block)
        if cached is not None:
            location = "cache"
            dirty = platter_seq is None or cached.seq > platter_seq
        else:
            location = "platter" if platter_seq is not None else "image"
            dirty = False
        failed = (
            self._last_failure is not None
            and self._last_failure.get("block") == block
            # the marker is only meaningful while the newest value is still
            # waiting in cache; once it lands (or cache is lost) it is done
            and location == "cache"
            and dirty
        )
        return {
            "block": block,
            "location": location,
            "dirty": dirty,
            "cache_seq": cached.seq if cached else None,
            "platter_seq": platter_seq,
            "undo_pending": undo_block == block,
            "failed": failed,
        }

    def state(self) -> dict:
        with self._lock:
            # display-only probe: keep it out of the I/O accounting
            undo = self._read_undo_locked(account=False)
            undo_block = undo[0] if undo is not None else None
            return {
                "block_size": BLOCK_SIZE,
                "block_count": self.block_count,
                "capacity": self.capacity,
                "next_seq": self._next_seq,
                "power_cycles": self._power_cycle_count,
                "image_path": self.image_path,
                "flush": self.session_state(),
                "fault": self.fault_state(),
                "last_failure": dict(self._last_failure) if self._last_failure else None,
                "undo": (
                    {"block": undo[0], "seq": undo[1]} if undo is not None else None
                ),
                "io": self.io_stats(),
                "blocks": [
                    self._block_state_locked(b, undo_block) for b in range(self.block_count)
                ],
                "events": list(self._log),
            }
