"""Flask front-end for the single-disk write-back cache simulator."""

from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request

from .disk import (
    BLOCK_SIZE,
    DiskError,
    FlushBusyError,
    WriteBackDisk,
)

IMAGE_PATH = os.environ.get("IMAGE_PATH", "/data/disk.img")
BLOCK_COUNT = int(os.environ.get("BLOCK_COUNT", "32"))
FLUSH_DELAY = float(os.environ.get("FLUSH_DELAY", "0.6"))

app = Flask(__name__)
disk = WriteBackDisk(
    image_path=IMAGE_PATH,
    block_count=BLOCK_COUNT,
    flush_delay=FLUSH_DELAY,
)


@app.errorhandler(DiskError)
def handle_disk_error(exc: DiskError):
    status = 409 if isinstance(exc, FlushBusyError) else 400
    return jsonify({"error": type(exc).__name__, "detail": str(exc)}), status


@app.get("/")
def index():
    return render_template("index.html", block_count=BLOCK_COUNT, block_size=BLOCK_SIZE)


@app.get("/api/state")
def get_state():
    return jsonify(disk.state())


def _decode_payload(body: dict) -> bytes:
    raw = body.get("data", "")
    if not isinstance(raw, str):
        raise DiskError("field 'data' must be a string")
    encoding = body.get("encoding", "utf-8")
    if encoding == "hex":
        try:
            payload = bytes.fromhex(raw)
        except ValueError as exc:
            raise DiskError(f"invalid hex payload: {exc}") from exc
    elif encoding in ("utf-8", "text", "ascii"):
        payload = raw.encode("utf-8")
    else:
        raise DiskError(f"unsupported encoding: {encoding}")

    if body.get("pad"):
        if len(payload) > BLOCK_SIZE:
            raise DiskError(
                f"payload ({len(payload)} bytes) larger than one {BLOCK_SIZE} byte block"
            )
        payload = payload.ljust(BLOCK_SIZE, b"\x00")
    elif len(payload) != BLOCK_SIZE:
        raise DiskError(
            f"whole-block writes need exactly {BLOCK_SIZE} bytes; got "
            f"{len(payload)} (send pad=true to zero-pad)"
        )
    return payload


@app.post("/api/write")
def write_block():
    body = request.get_json(force=True, silent=True) or {}
    raw_block = body.get("block")
    # reject bool (JSON true/false) and non-integer block indices explicitly
    if isinstance(raw_block, bool) or not isinstance(raw_block, int):
        raise DiskError("field 'block' must be an integer block index")
    block = raw_block
    payload = _decode_payload(body)
    fua = bool(body.get("fua", False))
    result = disk.write(block, payload, fua=fua)
    return jsonify({"ok": True, "result": result, "state": disk.state()})


@app.get("/api/read")
def read_block():
    raw_block = request.args.get("block", "")
    if isinstance(raw_block, bool) or not str(raw_block).lstrip("-").isdigit():
        raise DiskError("query parameter 'block' must be an integer")
    block = int(raw_block)
    result = disk.read(block)
    data: bytes = result["data"]
    return jsonify(
        {
            "ok": True,
            "result": {
                "block": result["block"],
                "source": result["source"],
                "seq": result["seq"],
                "data_hex": data.hex(),
                "preview": _preview(data),
            },
        }
    )


def _preview(data: bytes) -> str:
    head = data.rstrip(b"\x00")
    if not head:
        return "(all zero)"
    try:
        return head.decode("utf-8")
    except UnicodeDecodeError:
        return head[:48].hex() + ("…" if len(data) > 48 else "")


@app.post("/api/flush")
def start_flush():
    body = request.get_json(force=True, silent=True) or {}
    selected = body.get("selected")
    if not isinstance(selected, list) or not selected:
        raise DiskError("field 'selected' must be a non-empty list of block indices")
    if any(isinstance(b, bool) for b in selected):
        raise DiskError("selected block indices must be integers, not booleans")
    try:
        selected = [int(b) for b in selected]
    except (TypeError, ValueError) as exc:
        raise DiskError("selected block indices must be integers") from exc
    session = disk.start_flush(selected)
    return jsonify({"ok": True, "flush": session, "state": disk.state()})


@app.post("/api/flush/pause")
def pause_flush():
    return jsonify({"ok": True, "flush": disk.pause_flush(), "state": disk.state()})


@app.post("/api/flush/resume")
def resume_flush():
    return jsonify({"ok": True, "flush": disk.resume_flush(), "state": disk.state()})


@app.post("/api/power-loss")
def power_loss():
    state = disk.power_loss()
    return jsonify({"ok": True, "state": state})


@app.post("/api/reset")
def reset():
    state = disk.reset()
    return jsonify({"ok": True, "state": state})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
