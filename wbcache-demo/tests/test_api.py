"""End-to-end HTTP smoke tests using Flask's test client."""

import json
import os
import tempfile
import time
import unittest

os.environ.setdefault("IMAGE_PATH", os.path.join(tempfile.mkdtemp(), "api.img"))
os.environ.setdefault("BLOCK_COUNT", "8")
os.environ.setdefault("FLUSH_DELAY", "0.4")

from app.server import app  # noqa: E402


def block_payload(tag: str) -> str:
    return tag.ljust(4096, ".").encode().hex()


class ApiTests(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.client = app.test_client()
        self.client.post("/api/reset")

    def post(self, path, body=None):
        resp = self.client.post(path, data=json.dumps(body or {}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.get_json()

    def get(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.get_json()

    def test_full_interleaved_scenario_over_http(self):
        # page renders
        self.assertIn(b"FLUSH", self.client.get("/").data)

        # two normal writes -> cache, image untouched
        self.post("/api/write", {"block": 0, "encoding": "hex", "data": block_payload("HTTP-v1")})
        self.post("/api/write", {"block": 1, "data": "HTTP-v2", "pad": True})
        state = self.get("/api/state")
        self.assertTrue(all(b["dirty"] for b in state["blocks"] if b["block"] in (0, 1)))

        r = self.get("/api/read?block=1")
        self.assertEqual(r["result"]["source"], "cache")
        self.assertTrue(r["result"]["preview"].startswith("HTTP-v2"))

        # start a one-round FLUSH and immediately write a newer value
        self.post("/api/flush", {"selected": [0, 1]})
        self.post("/api/write", {"block": 1, "data": "HTTP-v3-after-accept", "pad": True})

        for _ in range(100):
            st = self.get("/api/state")["flush"]
            if st["status"] == "done":
                break
            time.sleep(0.02)
        self.assertEqual(st["status"], "done")

        # block 0 persisted; block 1 image holds the snapshot, cache holds v3
        r0 = self.get("/api/read?block=0")
        self.assertEqual(r0["result"]["source"], "platter")
        r1 = self.get("/api/read?block=1")
        self.assertEqual(r1["result"]["source"], "cache")
        self.assertTrue(r1["result"]["preview"].startswith("HTTP-v3-after-accept"))

        # FUA during a paused round: two dirty blocks, pause after the first
        # one has landed so the second still has a pending snapshot
        self.post("/api/write", {"block": 2, "data": "round2-b2", "pad": True})
        self.post("/api/write", {"block": 3, "data": "round2-b3", "pad": True})
        self.post("/api/flush", {"selected": [2, 3]})
        for _ in range(100):
            s = self.get("/api/state")["flush"]
            if s["cursor"] == 1 and s["status"] == "running":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("pause window never appeared")
        self.post("/api/flush/pause")
        fua = self.post("/api/write",
                        {"block": 3, "data": "round2-fua", "pad": True, "fua": True})
        self.assertTrue(fua["result"]["persisted"])
        self.post("/api/flush/resume")
        for _ in range(100):
            st = self.get("/api/state")["flush"]
            if st["status"] == "done":
                break
            time.sleep(0.02)
        # JSON object keys are strings on the HTTP boundary
        self.assertEqual(st["results"]["3"], "skipped-stale")
        r3 = self.get("/api/read?block=3")
        self.assertTrue(r3["result"]["preview"].startswith("round2-fua"))

        # power loss: start a multi-block round, wait until part of it has
        # landed, then cut power while the round is still in flight
        self.post("/api/write", {"block": 4, "data": "persisted-before-loss", "pad": True})
        self.post("/api/write", {"block": 5, "data": "lost-on-powercut", "pad": True})
        self.post("/api/write", {"block": 6, "data": "also-lost", "pad": True})
        self.post("/api/flush", {"selected": [4, 5, 6]})
        for _ in range(100):
            s = self.get("/api/state")["flush"]
            if s["cursor"] == 1 and s["status"] == "running":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("pre-power-loss window never appeared")
        pl = self.post("/api/power-loss")
        self.assertEqual(pl["state"]["flush"]["status"], "aborted")
        # block 4 reached the image and survives; 5/6 only existed in cache
        r4 = self.get("/api/read?block=4")
        self.assertTrue(r4["result"]["preview"].startswith("persisted-before-loss"))
        r5 = self.get("/api/read?block=5")
        self.assertEqual(r5["result"]["source"], "image")
        self.assertIn("INIT", r5["result"]["preview"])
        r6 = self.get("/api/read?block=6")
        self.assertIn("INIT", r6["result"]["preview"])
        # persisted FUA data survives
        r3b = self.get("/api/read?block=3")
        self.assertTrue(r3b["result"]["preview"].startswith("round2-fua"))

    def test_validation_errors(self):
        resp = self.client.post("/api/write", json={"block": 0, "data": "short", "pad": False})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/write", json={"block": True, "data": "x", "pad": True})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/read?block=abc")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/flush", json={"selected": []})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/flush", json={"selected": [True]})
        self.assertEqual(resp.status_code, 400)
        # two blocks keep the round in flight long enough with FLUSH_DELAY
        self.post("/api/write", {"block": 0, "data": "x", "pad": True})
        self.post("/api/write", {"block": 1, "data": "y", "pad": True})
        self.post("/api/flush", {"selected": [0, 1]})
        resp = self.client.post("/api/flush", json={"selected": [0]})
        self.assertEqual(resp.status_code, 409)

    def test_short_write_flow_over_http(self):
        # write three dirty blocks, arm a short write on the middle one
        self.post("/api/write", {"block": 0, "data": "b0-new", "pad": True})
        self.post("/api/write", {"block": 1, "data": "b1-new", "pad": True})
        self.post("/api/write", {"block": 2, "data": "b2-new", "pad": True})
        armed = self.post("/api/fault/short-write",
                          {"block": 1, "bytes": 1024, "persistent": False})
        self.assertIn("1", armed["state"]["fault"]["short_write"])
        bad = self.client.post("/api/fault/short-write", json={"block": "x"})
        self.assertEqual(bad.status_code, 400)

        started = self.post("/api/flush", {"selected": [0, 1, 2]})
        self.assertEqual(started["flush"]["status"], "running")
        for _ in range(200):
            st = self.get("/api/state")["flush"]
            if st["status"] in ("failed", "done", "aborted"):
                break
            time.sleep(0.02)
        self.assertEqual(st["status"], "failed")
        self.assertEqual(st["failed_block"], 1)
        self.assertEqual(st["results"]["0"], "persisted")
        self.assertEqual(st["results"]["1"], "failed-short-write")
        self.assertNotIn("2", st["results"])

        state = self.get("/api/state")
        blocks = {b["block"]: b for b in state["blocks"]}
        self.assertTrue(blocks[1]["dirty"] and blocks[1]["failed"])
        self.assertTrue(blocks[2]["dirty"] and not blocks[2]["failed"])
        self.assertFalse(blocks[0]["dirty"])

        # the page can show the failed block and inject the fault
        page = self.client.get("/").data
        self.assertIn(b"/api/fault/short-write", page)
        self.assertIn(b"failedLine", page)

        # reads: block 0 from platter, failed/unprocessed blocks from cache
        self.assertEqual(self.get("/api/read?block=0")["result"]["source"], "platter")
        r1 = self.get("/api/read?block=1")["result"]
        self.assertEqual(r1["source"], "cache")
        self.assertTrue(r1["preview"].startswith("b1-new"))
        r2 = self.get("/api/read?block=2")["result"]
        self.assertEqual(r2["source"], "cache")
        self.assertTrue(r2["preview"].startswith("b2-new"))

        # simulate power loss: the image keeps block 1's original value
        self.post("/api/power-loss")
        r1 = self.get("/api/read?block=1")["result"]
        self.assertEqual(r1["source"], "image")
        self.assertIn("INIT", r1["preview"])
        self.assertTrue(self.get("/api/read?block=0")["result"]["preview"].startswith("b0-new"))

        # a fresh round is possible after the failure and fixes things
        self.post("/api/write", {"block": 5, "data": "after-retry", "pad": True})
        self.post("/api/flush", {"selected": [5]})
        for _ in range(200):
            st = self.get("/api/state")["flush"]
            if st["status"] in ("failed", "done", "aborted"):
                break
            time.sleep(0.02)
        self.assertEqual(st["status"], "done")
        self.assertTrue(self.get("/api/read?block=5")["result"]["preview"].startswith("after-retry"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
