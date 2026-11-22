"""Unit tests for the write-back cache semantics (stdlib unittest only)."""

import os
import tempfile
import time
import unittest

from app.disk import (
    BLOCK_SIZE,
    FLUSH_ABORTED,
    FLUSH_DONE,
    FLUSH_PAUSED,
    FLUSH_RUNNING,
    BlockError,
    FlushBusyError,
    WriteBackDisk,
)


def payload(tag: str) -> bytes:
    return tag.encode().ljust(BLOCK_SIZE, b".")


def wait_for(disk, statuses, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = disk.session_state()
        if st["status"] in statuses:
            return st
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {statuses}, got {disk.session_state()}")


class WriteBackDiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = os.path.join(self.tmp.name, "disk.img")
        # no inter-block delay in tests
        self.disk = WriteBackDisk(self.img, block_count=8, flush_delay=0)

    def test_normal_write_answers_from_cache_and_reads_newest(self):
        self.disk.write(1, payload("v1"))
        self.disk.write(1, payload("v2"))
        r = self.disk.read(1)
        self.assertEqual(r["source"], "cache")
        self.assertEqual(r["data"], payload("v2"))
        # nothing persisted yet: raw image still holds the initial marker
        with open(self.img, "rb") as fh:
            fh.seek(BLOCK_SIZE)
            on_disk = fh.read(BLOCK_SIZE)
        self.assertTrue(on_disk.startswith(b"INIT-BLOCK-01"))

    def test_whole_block_only(self):
        with self.assertRaises(BlockError):
            self.disk.write(0, b"too short")
        with self.assertRaises(BlockError):
            self.disk.write(99, payload("x"))
        with self.assertRaises(BlockError):
            self.disk.read(-1)
        with self.assertRaises(BlockError):
            self.disk.write(True, payload("x"))
        with self.assertRaises(BlockError):
            self.disk.start_flush([False])

    def test_flush_persists_snapshot_and_clears_cache(self):
        self.disk.write(2, payload("A"))
        self.disk.write(3, payload("B"))
        state = self.disk.start_flush([2, 3])
        self.assertEqual(state["status"], FLUSH_RUNNING)
        final = wait_for(self.disk, {FLUSH_DONE})
        self.assertEqual(final["cursor"], 2)
        self.assertEqual(self.disk.read(2)["source"], "platter")
        self.assertEqual(self.disk.read(3)["source"], "platter")
        with open(self.img, "rb") as fh:
            fh.seek(2 * BLOCK_SIZE)
            self.assertEqual(fh.read(BLOCK_SIZE), payload("A"))

    def test_write_after_flush_accept_does_not_ride_this_round(self):
        self.disk.write(4, payload("old"))
        self.disk.start_flush([4])
        # accepted while the round is in flight; must stay cached afterwards
        self.disk.write(4, payload("new"))
        wait_for(self.disk, {FLUSH_DONE})
        r = self.disk.read(4)
        self.assertEqual(r["source"], "cache")  # newer value still pending
        self.assertEqual(r["data"], payload("new"))
        with open(self.img, "rb") as fh:
            fh.seek(4 * BLOCK_SIZE)
            self.assertEqual(fh.read(BLOCK_SIZE), payload("old"))  # snapshot only

    def test_only_one_flush_round_at_a_time(self):
        # dedicated disk with a slow round so a second accept is attempted
        # while the first one is genuinely still running
        busy_img = os.path.join(self.tmp.name, "busy.img")
        busy = WriteBackDisk(busy_img, block_count=8, flush_delay=0.15)
        busy.write(0, payload("x"))
        busy.start_flush([0])
        with self.assertRaises(FlushBusyError):
            busy.start_flush([0])
        wait_for(busy, {FLUSH_DONE})
        # after completion a new round is allowed
        busy.write(1, payload("y"))
        busy.start_flush([1])
        wait_for(busy, {FLUSH_DONE})

    def test_pause_keeps_rw_working_and_fua_persists_one_block(self):
        self.disk.write(0, payload("a0"))
        self.disk.write(1, payload("b0"))
        self.disk.write(2, payload("c0"))
        self.disk.start_flush([0, 1, 2])
        self.disk.pause_flush()
        self.assertEqual(self.disk.session_state()["status"], FLUSH_PAUSED)

        # ordinary reads/writes continue normally while paused
        self.disk.write(2, payload("c1"))
        self.assertEqual(self.disk.read(2)["data"], payload("c1"))

        # FUA on block 1: that block lands immediately, nothing else does
        fua = self.disk.write(1, payload("b1-fua"), fua=True)
        self.assertTrue(fua["persisted"])
        with open(self.img, "rb") as fh:
            fh.seek(1 * BLOCK_SIZE)
            self.assertEqual(fh.read(BLOCK_SIZE), payload("b1-fua"))
            fh.seek(0)
            self.assertTrue(fh.read(BLOCK_SIZE).startswith(b"INIT-BLOCK-00"))

        # a second pause request while paused is rejected
        with self.assertRaises(FlushBusyError):
            self.disk.pause_flush()

        self.disk.resume_flush()
        final = wait_for(self.disk, {FLUSH_DONE})
        # block 1 snapshot is older than the FUA value on the platter
        self.assertEqual(final["results"][1], "skipped-stale")
        with open(self.img, "rb") as fh:
            fh.seek(1 * BLOCK_SIZE)
            self.assertEqual(fh.read(BLOCK_SIZE), payload("b1-fua"))  # not overwritten
            fh.seek(0)
            self.assertEqual(fh.read(BLOCK_SIZE), payload("a0"))
        # the post-accept write on block 2 was not part of the round
        self.assertEqual(self.disk.read(2)["source"], "cache")
        self.assertEqual(self.disk.read(2)["data"], payload("c1"))

    def test_power_loss_drops_cache_aborts_flush_and_image_survives(self):
        # dedicated disk with a measurable per-block delay so the round can be
        # cut while still in flight
        slow_img = os.path.join(self.tmp.name, "slow.img")
        slow = WriteBackDisk(slow_img, block_count=8, flush_delay=0.15)

        slow.write(0, payload("persisted-soon"))
        slow.write(5, payload("stuck-in-cache"))
        slow.start_flush([0, 5])
        # wait until block 0 is on the platter but block 5 is not yet handled
        deadline = time.time() + 5
        while time.time() < deadline:
            st = slow.session_state()
            if 0 in st["results"] and 5 not in st["results"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("flush timing window never appeared")

        slow.power_loss()

        st = slow.session_state()
        self.assertEqual(st["status"], FLUSH_ABORTED)
        # block 5 was never persisted: image keeps its initial content
        r5 = slow.read(5)
        self.assertEqual(r5["source"], "image")
        self.assertTrue(r5["data"].startswith(b"INIT-BLOCK-05"))
        # block 0 had reached the image: it survives
        r0 = slow.read(0)
        self.assertEqual(r0["data"], payload("persisted-soon"))
        # no flush can be resumed after power loss
        with self.assertRaises(FlushBusyError):
            slow.resume_flush()
        # a fresh round works on the post-loss disk
        slow.write(6, payload("after-loss"))
        slow.start_flush([6])
        wait_for(slow, {FLUSH_DONE})
        self.assertEqual(slow.read(6)["source"], "platter")

    def test_fua_does_not_persist_other_dirty_blocks(self):
        self.disk.write(0, payload("x"))
        self.disk.write(1, payload("y"))
        self.disk.write(1, payload("y-fua"), fua=True)
        r0 = self.disk.read(0)
        self.assertEqual(r0["source"], "cache")
        with open(self.img, "rb") as fh:
            fh.seek(0)
            self.assertTrue(fh.read(BLOCK_SIZE).startswith(b"INIT-BLOCK-00"))

    def test_unselected_dirty_blocks_remain_cached_after_flush(self):
        self.disk.write(0, payload("in"))
        self.disk.write(1, payload("out"))
        self.disk.start_flush([0])
        wait_for(self.disk, {FLUSH_DONE})
        self.assertEqual(self.disk.read(0)["source"], "platter")
        self.assertEqual(self.disk.read(1)["source"], "cache")

    def test_clean_selected_block_is_skipped(self):
        self.disk.write(0, payload("x"))
        self.disk.start_flush([0])
        wait_for(self.disk, {FLUSH_DONE})
        self.disk.start_flush([0, 1])  # both clean now (1 never written)
        final = wait_for(self.disk, {FLUSH_DONE})
        self.assertEqual(final["results"][0], "skipped-clean")
        self.assertEqual(final["results"][1], "skipped-clean")


if __name__ == "__main__":
    unittest.main(verbosity=2)
