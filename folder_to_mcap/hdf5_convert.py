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

try:
    import hdf5plugin  # noqa: F401  (importing registers third-party filters, e.g. Zstandard)
except ImportError:
    log.warning(
        "hdf5plugin is not installed; datasets compressed with third-party filters "
        "(e.g. Zstandard) will fail to read with an obscure 'can't open directory' "
        "OSError. Install with: pip install hdf5plugin"
    )


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


_MAX_U64 = (1 << 64) - 1


def _is_valid_ns(value) -> bool:
    """MCAP timestamps are packed as unsigned 64-bit integers; some records
    carry a negative or otherwise out-of-range sentinel in
    header.data_timestamp.ns.m_value (e.g. "no timestamp set")."""
    return isinstance(value, (int, float)) and 0 <= value <= _MAX_U64


def _extract_timestamp_ns(record_dict: dict, frame_time_sec: float) -> int:
    header = record_dict.get("header")
    if isinstance(header, dict):
        ts = header.get("data_timestamp")
        if isinstance(ts, dict):
            ns = ts.get("ns")
            if isinstance(ns, dict):
                value = ns.get("m_value")
                if value and _is_valid_ns(value):
                    return int(value)
    fallback_ns = round(frame_time_sec * 1_000_000_000)
    if not _is_valid_ns(fallback_ns):
        raise ValueError(f"no valid timestamp available (frame_time_sec={frame_time_sec!r})")
    return int(fallback_ns)


class _McapOutput:
    """One open MCAP writer, with lazy per-topic channel registration (a
    schema/channel is only created in this specific file the first time a
    message actually needs to be written to it)."""

    def __init__(self, path: Path):
        self._f = open(path, "wb")
        self.writer = Writer(self._f, compression=CompressionType.ZSTD)
        self.writer.start(profile="", library="folder_to_mcap-hdf5")
        self._schema_id = None
        self._channel_ids: dict[str, int] = {}

    def _schema(self) -> int:
        if self._schema_id is None:
            self._schema_id = self.writer.register_schema(
                name="hdf5_signal",
                encoding="jsonschema",
                data=json.dumps({"type": "object"}).encode("utf-8"),
            )
        return self._schema_id

    def channel(self, topic: str) -> int:
        if topic not in self._channel_ids:
            self._channel_ids[topic] = self.writer.register_channel(
                topic=topic, message_encoding="json", schema_id=self._schema()
            )
        return self._channel_ids[topic]

    def finish(self):
        self.writer.finish()
        self._f.close()


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


_DEFAULT_EPOCH_CUTOFF_SEC = 946684800.0  # 2000-01-01T00:00:00Z


def convert(
    input_path: Path,
    output_path: Path,
    limit: int | None = None,
    epoch_output_path: Path | None = None,
    epoch_cutoff_sec: float = _DEFAULT_EPOCH_CUTOFF_SEC,
):
    """Convert every signal in input_path into output_path. If
    epoch_output_path is given, messages whose resolved timestamp falls
    before epoch_cutoff_sec (default: year 2000) -- i.e. records that only
    had a placeholder/zero frame.time available, not a real one -- are
    routed into that separate file instead, keeping the main file's time
    range meaningful."""
    epoch_cutoff_ns = round(epoch_cutoff_sec * 1_000_000_000)

    with h5py.File(input_path, "r") as h5file:
        main_out = _McapOutput(output_path)
        epoch_out = _McapOutput(epoch_output_path) if epoch_output_path is not None else None
        try:
            signal_paths = sorted(find_signal_groups(h5file))
            log.info("found %d signal groups", len(signal_paths))

            total_messages = 0
            total_epoch_messages = 0
            for i, path in enumerate(signal_paths):
                group = h5file[path]
                data = group["data"]
                frame = group["frame"]
                n = min(len(data), len(frame))
                if limit is not None:
                    n = min(n, limit)

                topic = "/" + path

                count = 0
                epoch_count = 0
                error_count = 0
                first_error = None
                consecutive_failures = 0
                max_consecutive_failures = 5
                for idx in range(n):
                    try:
                        record_dict = _record_to_dict(data[idx])
                        ts_ns = _extract_timestamp_ns(record_dict, float(frame[idx]["time"]))
                        target = epoch_out if (epoch_out is not None and ts_ns < epoch_cutoff_ns) else main_out
                        target.writer.add_message(
                            channel_id=target.channel(topic),
                            log_time=ts_ns,
                            publish_time=ts_ns,
                            data=json.dumps(record_dict).encode("utf-8"),
                        )
                        if target is epoch_out:
                            epoch_count += 1
                    except Exception as e:
                        error_count += 1
                        consecutive_failures += 1
                        if first_error is None:
                            first_error = f"{path}[{idx}]: {e}"
                        if consecutive_failures >= max_consecutive_failures:
                            break
                        continue
                    consecutive_failures = 0
                    count += 1
                total_messages += count
                total_epoch_messages += epoch_count

                epoch_note = f", {epoch_count} to epoch file" if epoch_count else ""
                if error_count:
                    log.warning(
                        "[%d/%d] wrote %d/%d messages for %s%s (%d failed; first error: %s)",
                        i + 1,
                        len(signal_paths),
                        count,
                        n,
                        topic,
                        epoch_note,
                        error_count,
                        first_error,
                    )
                else:
                    log.info(
                        "[%d/%d] wrote %d messages for %s%s", i + 1, len(signal_paths), count, topic, epoch_note
                    )
        finally:
            main_out.finish()
            if epoch_out is not None:
                epoch_out.finish()

    log.info("wrote %s (%d topics, %d messages)", output_path, len(signal_paths), total_messages - total_epoch_messages)
    if epoch_output_path is not None:
        log.info("wrote %s (%d messages before %s)", epoch_output_path, total_epoch_messages, epoch_cutoff_sec)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="Path to the .h5/.hdf5 file")
    parser.add_argument("--output", required=True, type=Path, help="Path to write the .mcap file")
    parser.add_argument(
        "--limit", type=int, default=None, help="Optional cap on records per signal (for quick testing)"
    )
    parser.add_argument(
        "--epoch-output",
        type=Path,
        default=None,
        help="Optional path for a second .mcap file. If given, messages whose resolved "
        "timestamp is before --epoch-cutoff (records that only had a placeholder/zero "
        "frame.time, not a real one) are written there instead of --output, so they "
        "don't stretch the main file's time range back to 1970.",
    )
    parser.add_argument(
        "--epoch-cutoff",
        type=float,
        default=_DEFAULT_EPOCH_CUTOFF_SEC,
        help="Unix seconds threshold used with --epoch-output: timestamps before this are "
        "considered placeholder/epoch time (default: %(default)s, i.e. year 2000)",
    )
    args = parser.parse_args()
    convert(args.input, args.output, args.limit, args.epoch_output, args.epoch_cutoff)


if __name__ == "__main__":
    main()
