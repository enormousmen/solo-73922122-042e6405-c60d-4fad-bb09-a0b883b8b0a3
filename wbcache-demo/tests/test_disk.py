"""Unit tests for the write-back cache semantics (stdlib unittest only)."""

import os
import tempfile
import time
import unittest
from unittest import mock

from app.disk import (
    BLOCK_SIZE,
    FLUSH_ABORTED,
    FLUSH_DONE,
    FLUSH_FAILED,
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


class ShortWriteTests(unittest.TestCase):
    """A torn block write ends the round and must never corrupt the image."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = os.path.join(self.tmp.name, "disk.img")
        self.disk = WriteBackDisk(self.img, block_count=8, flush_delay=0)

    def read_image_block(self, block: int) -> bytes:
        with open(self.img, "rb") as fh:
            fh.seek(block * BLOCK_SIZE)
            return fh.read(BLOCK_SIZE)

    def test_short_write_ends_round_and_reports_failed_block(self):
        for b in (0, 1, 2):
            self.disk.write(b, payload(f"new-{b}"))
        self.disk.arm_short_write(1, 1024, persistent=True)
        self.disk.start_flush([0, 1, 2])

        final = wait_for(self.disk, {FLUSH_FAILED})
        self.assertEqual(final["status"], FLUSH_FAILED)
        self.assertEqual(final["failed_block"], 1)
        self.assertEqual(final["results"][0], "persisted")
        self.assertEqual(final["results"][1], "failed-short-write")
        self.assertNotIn(2, final["results"])  # round stops, block 2 untouched
        self.assertEqual(final["cursor"], 1)

        # already-flushed block keeps its platter value
        self.assertEqual(self.disk.read(0)["source"], "platter")
        self.assertEqual(self.disk.read(0)["data"], payload("new-0"))
        # failed and unprocessed blocks keep their newest cache values
        self.assertEqual(self.disk.read(1)["source"], "cache")
        self.assertEqual(self.disk.read(1)["data"], payload("new-1"))
        self.assertEqual(self.disk.read(2)["source"], "cache")
        self.assertEqual(self.disk.read(2)["data"], payload("new-2"))

    def test_failed_block_image_keeps_original_complete_value(self):
        for b in (0, 1):
            self.disk.write(b, payload(f"new-{b}"))
        before = self.read_image_block(1)
        self.disk.arm_short_write(1, 1536, persistent=True)
        self.disk.start_flush([0, 1])
        wait_for(self.disk, {FLUSH_FAILED})

        # The torn write only touched the prefix; rollback leaves a complete,
        # byte-identical original block in the image.
        self.assertEqual(self.read_image_block(1), before)
        self.assertTrue(before.startswith(b"INIT-BLOCK-01"))
        self.assertIsNone(self.disk.state()["undo"])
        # A restart (new object over the same image) reads the original value.
        restarted = WriteBackDisk(self.img, block_count=8, flush_delay=0)
        self.assertEqual(restarted.read(1)["data"], before)
        self.assertEqual(restarted.read(1)["source"], "image")

    def test_power_loss_after_short_write_keeps_original_value(self):
        self.disk.write(1, payload("never-lands"))
        before = self.read_image_block(1)
        self.disk.arm_short_write(1, 2048, persistent=True)
        self.disk.start_flush([1])
        wait_for(self.disk, {FLUSH_FAILED})
        self.disk.power_loss()

        self.assertEqual(self.disk.read(1)["source"], "image")
        self.assertEqual(self.disk.read(1)["data"], before)
        restarted = WriteBackDisk(self.img, block_count=8, flush_delay=0)
        self.assertEqual(restarted.read(1)["data"], before)

    def test_retry_after_fault_cleared_persists_newest_cache_value(self):
        self.disk.write(1, payload("v1"))
        self.disk.arm_short_write(1, 512, persistent=True)
        self.disk.start_flush([1])
        wait_for(self.disk, {FLUSH_FAILED})
        # the block is still dirty in cache with its newest value
        st = self.disk.state()
        self.assertTrue(st["blocks"][1]["dirty"])
        self.assertTrue(st["blocks"][1]["failed"])
        # still armed: a retry without clearing the fault fails again
        self.disk.start_flush([1])
        wait_for(self.disk, {FLUSH_FAILED})
        self.disk.clear_faults()
        self.disk.write(1, payload("v2"))  # newest cached value
        self.disk.start_flush([1])
        final = wait_for(self.disk, {FLUSH_DONE})
        self.assertEqual(final["results"][1], "persisted")
        self.assertEqual(self.disk.read(1)["source"], "platter")
        self.assertEqual(self.read_image_block(1), payload("v2"))

    def test_one_shot_fault_only_affects_one_attempt(self):
        self.disk.write(1, payload("x"))
        self.disk.arm_short_write(1, 100, persistent=False)
        self.disk.start_flush([1])
        wait_for(self.disk, {FLUSH_FAILED})
        self.disk.start_flush([1])
        final = wait_for(self.disk, {FLUSH_DONE})
        self.assertEqual(final["results"][1], "persisted")

    def test_unselected_dirty_blocks_survive_a_failed_round(self):
        self.disk.write(0, payload("in-round"))
        self.disk.write(1, payload("fails"))
        self.disk.write(2, payload("not-selected"))
        self.disk.arm_short_write(1, 256, persistent=True)
        self.disk.start_flush([0, 1])
        wait_for(self.disk, {FLUSH_FAILED})
        self.assertEqual(self.disk.read(2)["source"], "cache")
        self.assertEqual(self.disk.read(2)["data"], payload("not-selected"))
        self.assertTrue(self.disk.state()["blocks"][2]["dirty"])

    def test_image_layout_stays_four_kib_blocks(self):
        before = os.path.getsize(self.img)
        self.disk.write(1, payload("x"))
        self.disk.arm_short_write(1, 512, persistent=True)
        self.disk.start_flush([1])
        wait_for(self.disk, {FLUSH_FAILED})
        self.assertEqual(os.path.getsize(self.img), before)
        self.assertEqual(before, 8 * BLOCK_SIZE)

    def test_rollback_blocked_by_persistent_fault_uses_undo_journal(self):
        self.disk.write(1, payload("new"))
        before = self.read_image_block(1)
        # Make the image itself permanently unwritable after the torn write, so
        # the normal prefix rollback cannot run at all.
        orig_write_prefix = self.disk._write_image_prefix

        def failing_prefix(block, data):
            raise OSError("media error")

        self.disk._write_image_prefix = failing_prefix
        self.disk.arm_short_write(1, 1024, persistent=True)
        self.disk.start_flush([1])
        final = wait_for(self.disk, {FLUSH_FAILED})
        self.assertEqual(final["results"][1], "failed-short-write")
        # the image is torn, but the journal holds the original block
        undo = self.disk.state()["undo"]
        self.assertEqual(undo["block"], 1)
        # the newest value is still cached, so a read serves the cache first
        self.assertEqual(self.disk.read(1)["source"], "cache")
        self.assertEqual(self.disk.read(1)["data"], payload("new"))
        # power loss clears the cache and tries to replay the journal; the
        # media error blocks the replay, so reads fall back to the journal
        self.disk.power_loss()
        self.assertEqual(self.disk.read(1)["source"], "undo")
        self.assertEqual(self.disk.read(1)["data"], before)
        self.assertEqual(self.disk.state()["undo"]["block"], 1)
        # A later power cycle, once the media works again, replays the journal
        # and makes the image hold the original complete block again.
        self.disk._write_image_prefix = orig_write_prefix
        self.disk.power_loss()
        self.assertIsNone(self.disk.state()["undo"])
        self.assertEqual(self.read_image_block(1), before)
        self.assertEqual(self.disk.read(1)["source"], "image")

    def test_extra_io_per_block_is_constant_in_image_capacity(self):
        def measure(block_count):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            path = os.path.join(tmp.name, "disk.img")
            disk = WriteBackDisk(path, block_count=block_count, flush_delay=0)
            disk.write(0, payload("x"))
            before = disk.io_stats()["total_bytes"]
            disk.start_flush([0])
            wait_for(disk, {FLUSH_DONE})
            return disk.io_stats()["total_bytes"] - before

        small = measure(8)
        large = measure(2048)
        self.assertEqual(small, large)
        # constant: one old-block read + one undo write + one block write,
        # plus the journal removal; independent of the number of blocks.
        self.assertLessEqual(small, 4 * BLOCK_SIZE + 64)

    def test_short_write_io_is_also_constant_in_capacity(self):
        def measure(block_count):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            path = os.path.join(tmp.name, "disk.img")
            disk = WriteBackDisk(path, block_count=block_count, flush_delay=0)
            disk.write(3, payload("torn"))
            disk.arm_short_write(3, 777, persistent=False)
            before = disk.io_stats()["total_bytes"]
            disk.start_flush([3])
            wait_for(disk, {FLUSH_FAILED})
            return disk.io_stats()["total_bytes"] - before

        small = measure(8)
        large = measure(4096)
        self.assertEqual(small, large)
        self.assertLessEqual(small, 5 * BLOCK_SIZE + 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
