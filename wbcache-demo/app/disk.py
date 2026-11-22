"""Write-back cache single-disk simulator.

The "disk" is a local image file addressed in whole 4 KiB blocks. Normal
writes complete from an in-memory cache; FLUSH persists a selected set of
dirty blocks over exactly one round (which can be paused), FUA writes force
the specified block to the platter before returning, and power loss drops the
cache and aborts any in-flight FLUSH, after which reads return the contents
of the original image again.

Concurrency model: a single reentrant lock guards all mutable state. The
FLUSH worker persists blocks one by one and waits on a pause event between
blocks; each persistence re-checks block versions so stale snapshots can
never overwrite a newer on-platter value.
"""

from __future__ import annotations

import collections
import os
import threading
import time
from dataclasses import dataclass, field

BLOCK_SIZE = 4096
DEFAULT_BLOCK_COUNT = 32

# FLUSH round lifecycle
FLUSH_IDLE = "idle"
FLUSH_RUNNING = "running"
FLUSH_PAUSED = "paused"
FLUSH_DONE = "done"
FLUSH_ABORTED = "aborted"


class DiskError(Exception):
    """Base error for invalid simulator operations."""


class BlockError(DiskError):
    """Invalid block index or payload."""


class FlushBusyError(DiskError):
    """A FLUSH round is already active."""


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
    # per-block outcome once handled: persisted | skipped-stale
    results: dict[int, str] = field(default_factory=dict)


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

        os.makedirs(os.path.dirname(os.path.abspath(image_path)) or ".", exist_ok=True)
        already_exists = os.path.exists(image_path)
        if image_initialized is None:
            image_initialized = already_exists and os.path.getsize(image_path) == self.capacity
        if not image_initialized:
            self._reset_image_file()
            if already_exists:
                self._log_event("image.recreated", "existing image had wrong size, recreated")
        elif not already_exists:
            self._reset_image_file()

    # ------------------------------------------------------------------ basics
    @property
    def capacity(self) -> int:
        return self.block_count * BLOCK_SIZE

    def _reset_image_file(self) -> None:
        with open(self.image_path, "wb") as fh:
            for block in range(self.block_count):
                marker = f"INIT-BLOCK-{block:02d}".encode().ljust(BLOCK_SIZE, b"\x00")
                fh.write(marker)
            fh.flush()
            os.fsync(fh.fileno())

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

    # ------------------------------------------------------------------ writes
    def _accept_write(self, block: int, data: bytes) -> int:
        """Apply a write to the cache and return its sequence number."""
        self._check_block(block)
        self._check_payload(data)
        seq = self._next_seq
        self._next_seq += 1
        self._cache[block] = CacheEntry(bytes(data), seq)
        return seq

    def _persist_locked(self, block: int, entry: CacheEntry, reason: str) -> None:
        """Write one block through to the image (caller holds the lock)."""
        with open(self.image_path, "r+b") as fh:
            fh.seek(block * BLOCK_SIZE)
            fh.write(entry.data)
            fh.flush()
            os.fsync(fh.fileno())
        self._persisted_version[block] = entry.seq
        cached = self._cache.get(block)
        if cached is not None and cached.seq == entry.seq:
            # nothing newer pending for this block: cache copy now redundant
            del self._cache[block]
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
                self._persist_locked(block, self._cache[block], "FUA")
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
        # cache miss: read the current image contents outside the lock
        with open(self.image_path, "rb") as fh:
            fh.seek(block * BLOCK_SIZE)
            data = fh.read(BLOCK_SIZE)
        if len(data) != BLOCK_SIZE:
            raise DiskError("image short read; image file may be corrupted")
        with self._lock:
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

                self._persist_locked(block, snapshot, f"FLUSH seq {session_seq}")
                session.results[block] = "persisted"
                session.cursor += 1

            # Pace the round so a human can pause between blocks.
            time.sleep(self.flush_delay)

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
            detail = (
                f"cache dropped; FLUSH seq {aborted.seq} aborted; "
                if aborted is not None
                else "cache dropped; no active FLUSH; "
            )
            self._log_event(
                "powerloss",
                detail + "reads now come from the original image again",
                aborted_seq=aborted.seq if aborted else None,
                power_cycle=self._power_cycle_count,
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
            self._next_seq = 1
            self._reset_image_file()
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
                "started_at": session.started_at,
                "finished_at": session.finished_at,
            }

    def _block_state_locked(self, block: int) -> dict:
        cached = self._cache.get(block)
        platter_seq = self._persisted_version.get(block)
        if cached is not None:
            location = "cache"
            dirty = platter_seq is None or cached.seq > platter_seq
        else:
            location = "platter" if platter_seq is not None else "image"
            dirty = False
        return {
            "block": block,
            "location": location,
            "dirty": dirty,
            "cache_seq": cached.seq if cached else None,
            "platter_seq": platter_seq,
        }

    def state(self) -> dict:
        with self._lock:
            return {
                "block_size": BLOCK_SIZE,
                "block_count": self.block_count,
                "capacity": self.capacity,
                "next_seq": self._next_seq,
                "power_cycles": self._power_cycle_count,
                "image_path": self.image_path,
                "flush": self.session_state(),
                "blocks": [self._block_state_locked(b) for b in range(self.block_count)],
                "events": list(self._log),
            }
