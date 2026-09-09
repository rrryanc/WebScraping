#!/usr/bin/env python3
"""Convert an HDF5 instrumentation log into an MCAP.

Expected layout (as produced by this vehicle software stack's log export):

    <root>/
      aos/activities/<activity>/outputs/<signal>/
        data                          # structured array, one record per sample
        frame                         # parallel array: time (float sec), index, msg_seq_number
        flags/                        # per-field validity flags (ignored)
        class_info_blob, class_info_json   # reflection metadata (ignored)

Every group containing both a `data` and a `frame` dataset is treated as one
signal and becomes its own MCAP topic, named after its HDF5 path (e.g.
aos/activities/corner_radar_front_left/outputs/detections/data becomes topic
/aos/activities/corner_radar_front_left/outputs/detections). Each `data`
record is converted, recursively, into a JSON-serializable dict. Since the
records use a custom nested C++ struct serialization (m_memory/m_union/
m_has_value/m_bitset wrappers), no attempt is made to interpret those
wrappers semantically -- fields are carried through with their literal
names, so all data is preserved and inspectable even though field names look
like internal implementation details.

Per-message timestamp: data.header.data_timestamp.ns.m_value (nanoseconds)
when present -- this matches the epoch used elsewhere in this project
(camera/lidar/trajectory timestamps) -- otherwise frame.time (float seconds)
scaled to nanoseconds.

Usage:
    python -m folder_to_mcap.hdf5_convert --input recording.h5 --output aos.mcap
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import h5py
import numpy as np
from mcap.writer import CompressionType, Writer

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("hdf5_to_mcap")


def _jsonify(value):
    """Recursively convert a numpy scalar/array/structured value into a
    JSON-serializable Python object. Structured (record) values keep their
    original field names; unions/optionals are carried through as plain
    dicts rather than being semantically interpreted."""
    if hasattr(value, "dtype") and value.dtype.names is not None:
        if isinstance(value, np.ndarray) and value.shape != ():
            return [_jsonify(row) for row in value]
        return {name: _jsonify(value[name]) for name in value.dtype.names}
    if isinstance(value, np.ndarray):
        return [_jsonify(x) for x in value]
    if isinstance(value, np.generic):
        return _jsonify(value.item())
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_jsonify(x) for x in value]
    return value


def _record_to_dict(record) -> dict:
    """Convert one structured-array row (a numpy record/void scalar) into a dict."""
    return {name: _jsonify(record[name]) for name in record.dtype.names}


def _extract_timestamp_ns(record_dict: dict, frame_time_sec: float) -> int:
    header = record_dict.get("header")
    if isinstance(header, dict):
        ts = header.get("data_timestamp")
        if isinstance(ts, dict):
            ns = ts.get("ns")
            if isinstance(ns, dict) and ns.get("m_value"):
                return int(ns["m_value"])
    return int(round(frame_time_sec * 1_000_000_000))


def find_signal_groups(h5file: h5py.File) -> list[str]:
    """Return the HDF5 paths of every group with both a `data` and a `frame`
    dataset as direct children."""
    found: list[str] = []

    def visitor(name, obj):
        if isinstance(obj, h5py.Group):
            data = obj.get("data")
            frame = obj.get("frame")
            if isinstance(data, h5py.Dataset) and isinstance(frame, h5py.Dataset):
                found.append(name)

    h5file.visititems(visitor)
    return found


def convert(input_path: Path, output_path: Path, limit: int | None = None):
    with h5py.File(input_path, "r") as h5file, open(output_path, "wb") as out_f:
        writer = Writer(out_f, compression=CompressionType.ZSTD)
        writer.start(profile="", library="folder_to_mcap-hdf5")

        signal_paths = sorted(find_signal_groups(h5file))
        log.info("found %d signal groups", len(signal_paths))

        schema_id = writer.register_schema(
            name="hdf5_signal",
            encoding="jsonschema",
            data=json.dumps({"type": "object"}).encode("utf-8"),
        )

        total_messages = 0
        for i, path in enumerate(signal_paths):
            group = h5file[path]
            data = group["data"]
            frame = group["frame"]
            n = min(len(data), len(frame))
            if limit is not None:
                n = min(n, limit)

            topic = "/" + path
            channel_id = writer.register_channel(topic=topic, message_encoding="json", schema_id=schema_id)

            count = 0
            consecutive_failures = 0
            max_consecutive_failures = 5
            for idx in range(n):
                try:
                    record_dict = _record_to_dict(data[idx])
                    ts_ns = _extract_timestamp_ns(record_dict, float(frame[idx]["time"]))
                    writer.add_message(
                        channel_id=channel_id,
                        log_time=ts_ns,
                        publish_time=ts_ns,
                        data=json.dumps(record_dict).encode("utf-8"),
                    )
                except Exception as e:
                    consecutive_failures += 1
                    if consecutive_failures == 1:
                        log.exception("failed to convert %s[%d]; skipping", path, idx)
                    else:
                        log.warning("failed to convert %s[%d]: %s", path, idx, e)
                    if consecutive_failures >= max_consecutive_failures:
                        log.warning(
                            "%s: %d consecutive failures, giving up on remaining %d records",
                            path,
                            consecutive_failures,
                            n - idx - 1,
                        )
                        break
                    continue
                consecutive_failures = 0
                count += 1
            total_messages += count
            log.info("[%d/%d] wrote %d messages for %s", i + 1, len(signal_paths), count, topic)

        writer.finish()
    log.info("wrote %s (%d topics, %d messages)", output_path, len(signal_paths), total_messages)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="Path to the .h5/.hdf5 file")
    parser.add_argument("--output", required=True, type=Path, help="Path to write the .mcap file")
    parser.add_argument(
        "--limit", type=int, default=None, help="Optional cap on records per signal (for quick testing)"
    )
    args = parser.parse_args()
    convert(args.input, args.output, args.limit)


if __name__ == "__main__":
    main()
