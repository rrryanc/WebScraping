"""Tests for the hdf5_convert conversion tool."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np
from mcap.reader import make_reader

from folder_to_mcap import hdf5_convert
from folder_to_mcap.hdf5_convert import convert, _extract_timestamp_ns, _record_to_dict

T0_NS = 1781767357923038976
DT_NS = 66670000


def _build_fixture(path: Path):
    with h5py.File(path, "w") as f:
        header_dtype = np.dtype(
            [
                ("data_timestamp", [("ns", [("m_value", "<i8")])]),
            ]
        )
        optional_u4 = np.dtype(
            [
                (
                    "m_memory",
                    {
                        "names": ["m_union", "m_has_value"],
                        "formats": [[("m_data", "<u4")], "?"],
                        "offsets": [0, 4],
                        "itemsize": 8,
                    },
                )
            ]
        )
        detection_dtype = np.dtype(
            [
                ("range_m", "<f4"),
                ("nested_optional", optional_u4),
                ("sub_array", "<f4", (3,)),
                ("label", "S8"),
            ]
        )
        data_dtype = np.dtype(
            [
                ("header", header_dtype),
                ("count", "<u4"),
                ("detections", detection_dtype, (2,)),
            ]
        )
        frame_dtype = np.dtype([("time", "<f8"), ("index", "<u4"), ("msg_seq_number", "<i8")])

        n = 4
        data = np.zeros(n, dtype=data_dtype)
        frame = np.zeros(n, dtype=frame_dtype)
        for i in range(n):
            ts_ns = T0_NS + i * DT_NS
            data[i]["header"]["data_timestamp"]["ns"]["m_value"] = ts_ns
            data[i]["count"] = 2
            for j in range(2):
                data[i]["detections"][j]["range_m"] = 10.0 + j
                data[i]["detections"][j]["nested_optional"]["m_memory"]["m_has_value"] = j % 2 == 0
                data[i]["detections"][j]["nested_optional"]["m_memory"]["m_union"]["m_data"] = j * 10
                data[i]["detections"][j]["sub_array"] = [j, j * 2, j * 3]
                data[i]["detections"][j]["label"] = f"obj{j}".encode()
            frame[i]["time"] = ts_ns / 1e9
            frame[i]["index"] = i
            frame[i]["msg_seq_number"] = 1000 + i

        grp = f.create_group("aos/activities/fake_radar/outputs/detections")
        grp.create_dataset("data", data=data)
        grp.create_dataset("frame", data=frame)
        grp.create_group("flags")
        grp.create_dataset("class_info_blob", data=np.zeros(10, dtype="u1"))
        grp.create_dataset("class_info_json", data=np.zeros(10, dtype="u1"))

        simple_dtype = np.dtype([("value", "<f8")])
        frame2 = np.zeros(3, dtype=frame_dtype)
        data2 = np.zeros(3, dtype=simple_dtype)
        for i in range(3):
            data2[i]["value"] = i * 1.5
            frame2[i]["time"] = 1781767357.0 + i
        grp2 = f.create_group("aos/activities/no_header_signal/outputs/simple")
        grp2.create_dataset("data", data=data2)
        grp2.create_dataset("frame", data=frame2)

        # Not a signal group (no data+frame pair): should be skipped.
        f.create_group("meta/settings")


class TestHdf5Convert(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.input_path = self.tmpdir / "input.h5"
        self.output_path = self.tmpdir / "out.mcap"
        _build_fixture(self.input_path)

    def test_finds_only_signal_groups(self):
        convert(self.input_path, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            topics = {ch.topic for ch in reader.get_summary().channels.values()}
            self.assertEqual(
                topics,
                {
                    "/aos/activities/fake_radar/outputs/detections",
                    "/aos/activities/no_header_signal/outputs/simple",
                },
            )

    def test_message_counts_match_record_counts(self):
        convert(self.input_path, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            counts = {
                ch.topic: reader.get_summary().statistics.channel_message_counts[ch_id]
                for ch_id, ch in reader.get_summary().channels.items()
            }
            self.assertEqual(counts["/aos/activities/fake_radar/outputs/detections"], 4)
            self.assertEqual(counts["/aos/activities/no_header_signal/outputs/simple"], 3)

    def test_uses_header_timestamp_when_present(self):
        convert(self.input_path, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            log_times = [
                message.log_time
                for _schema, channel, message in reader.iter_messages(
                    topics=["/aos/activities/fake_radar/outputs/detections"]
                )
            ]
            self.assertEqual(sorted(log_times), [T0_NS + i * DT_NS for i in range(4)])

    def test_falls_back_to_frame_time_without_header(self):
        convert(self.input_path, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            log_times = [
                message.log_time
                for _schema, channel, message in reader.iter_messages(
                    topics=["/aos/activities/no_header_signal/outputs/simple"]
                )
            ]
            self.assertEqual(sorted(log_times), [1781767357_000000000 + i * 1_000000000 for i in range(3)])

    def test_nested_unions_and_sub_arrays_decode(self):
        convert(self.input_path, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            _schema, _channel, message = next(
                reader.iter_messages(topics=["/aos/activities/fake_radar/outputs/detections"])
            )
            obj = json.loads(message.data)
            self.assertEqual(obj["count"], 2)
            det0, det1 = obj["detections"]
            self.assertAlmostEqual(det0["range_m"], 10.0)
            self.assertEqual(det0["label"], "obj0")
            self.assertEqual(det0["sub_array"], [0.0, 0.0, 0.0])
            self.assertTrue(det0["nested_optional"]["m_memory"]["m_has_value"])
            self.assertFalse(det1["nested_optional"]["m_memory"]["m_has_value"])
            self.assertEqual(det1["sub_array"], [1.0, 2.0, 3.0])

    def test_gives_up_on_signal_after_consecutive_failures(self):
        # A signal with 10 records where records 2-7 (six in a row) fail to
        # convert should stop after 5 consecutive failures rather than
        # grinding through the rest -- record 8/9 (which would succeed) are
        # never reached, and only the first two successful records land in
        # the MCAP.
        simple_dtype = np.dtype([("value", "<f8")])
        frame_dtype = np.dtype([("time", "<f8"), ("index", "<u4"), ("msg_seq_number", "<i8")])
        n = 10
        data = np.zeros(n, dtype=simple_dtype)
        frame = np.zeros(n, dtype=frame_dtype)
        for i in range(n):
            data[i]["value"] = float(i)
            frame[i]["time"] = 1781767357.0 + i

        flaky_path = self.tmpdir / "flaky.h5"
        with h5py.File(flaky_path, "w") as f:
            grp = f.create_group("aos/activities/flaky/outputs/signal")
            grp.create_dataset("data", data=data)
            grp.create_dataset("frame", data=frame)

        call_count = {"n": 0}

        def flaky_record_to_dict(record):
            call_count["n"] += 1
            if 3 <= call_count["n"] <= 8:
                raise ValueError("simulated failure")
            return _record_to_dict(record)

        with patch.object(hdf5_convert, "_record_to_dict", side_effect=flaky_record_to_dict):
            convert(flaky_path, self.output_path)

        self.assertEqual(call_count["n"], 7)  # 2 successes + 5 consecutive failures, then gave up
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            messages = list(reader.iter_messages(topics=["/aos/activities/flaky/outputs/signal"]))
            self.assertEqual(len(messages), 2)
            values = sorted(json.loads(m.data)["value"] for _s, _c, m in messages)
            self.assertEqual(values, [0.0, 1.0])

    def test_reads_zstandard_compressed_signals(self):
        # Real recordings use Zstandard (a third-party HDF5 filter, id 32015)
        # compression; hdf5_convert must import hdf5plugin so h5py can decode
        # it, otherwise every read fails with an obscure "can't open
        # directory" OSError regardless of which record is requested.
        import hdf5plugin

        simple_dtype = np.dtype([("value", "<f8")])
        frame_dtype = np.dtype([("time", "<f8"), ("index", "<u4"), ("msg_seq_number", "<i8")])
        n = 5
        data = np.zeros(n, dtype=simple_dtype)
        frame = np.zeros(n, dtype=frame_dtype)
        for i in range(n):
            data[i]["value"] = i * 2.5
            frame[i]["time"] = 1781767357.0 + i

        zstd_path = self.tmpdir / "zstd.h5"
        with h5py.File(zstd_path, "w") as f:
            grp = f.create_group("aos/activities/compressed_activity/outputs/compressed_signal")
            grp.create_dataset("data", data=data, chunks=(1,), **hdf5plugin.Zstd())
            grp.create_dataset("frame", data=frame)

        convert(zstd_path, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            messages = list(
                reader.iter_messages(topics=["/aos/activities/compressed_activity/outputs/compressed_signal"])
            )
            self.assertEqual(len(messages), 5)
            values = sorted(json.loads(m.data)["value"] for _s, _c, m in messages)
            self.assertEqual(values, [0.0, 2.5, 5.0, 7.5, 10.0])

    def test_negative_header_timestamp_falls_back_to_frame_time(self):
        # Some real records carry a negative sentinel in
        # header.data_timestamp.ns.m_value (e.g. "no timestamp set"), which
        # can't be packed as MCAP's unsigned 64-bit timestamp. That should
        # fall back to frame.time rather than raising.
        record = {"header": {"data_timestamp": {"ns": {"m_value": -1}}}}
        self.assertEqual(_extract_timestamp_ns(record, 1781767357.0), 1781767357_000000000)

    def test_valid_header_timestamp_is_used(self):
        record = {"header": {"data_timestamp": {"ns": {"m_value": T0_NS}}}}
        self.assertEqual(_extract_timestamp_ns(record, 0.0), T0_NS)

    def test_no_valid_timestamp_raises(self):
        with self.assertRaises(ValueError):
            _extract_timestamp_ns({}, -1.0)


if __name__ == "__main__":
    unittest.main()
