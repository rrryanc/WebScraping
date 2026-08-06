"""Tests for the folder_to_mcap conversion tool."""

import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from mcap.reader import make_reader
from PIL import Image
from pypcd4 import PointCloud

from folder_to_mcap.convert import convert

SEQ = "WV1ZZZEB1PH000490_20260618_072239-30s"
NEAR_SEQ = "WV1ZZZEB1PH000490_20260618_083145-30s"
VIN = "WV1ZZZEB1PH000490"
T0 = 1781767357923038976
DT = 66670000


def _build_fixture(root: Path, num_frames: int = 3):
    ts = [T0 + i * DT for i in range(num_frames)]

    traj_dir = root / "trajectory" / SEQ
    traj_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "tx": np.linspace(33.4, 33.0, num_frames),
            "ty": np.linspace(9.9, 9.5, num_frames),
            "tz": [0.87] * num_frames,
            "qx": [-0.0142] * num_frames,
            "qy": [0.0011] * num_frames,
            "qz": [0.945] * num_frames,
            "qw": [-0.326] * num_frames,
        },
        index=pd.Index(ts, name="timestamp_ns"),
    ).to_parquet(traj_dir / "trajectory.parquet")

    for cam in ["FC1", "TVfront"]:
        cam_dir = root / "camera" / cam / SEQ
        cam_dir.mkdir(parents=True)
        rows = []
        for i, t in enumerate(ts):
            fn = f"{i:06d}.jpg"
            Image.new("RGB", (8, 6), color=(i * 40 % 255, 10, 20)).save(cam_dir / fn, format="JPEG")
            rows.append({"filename": f"camera/{cam}/{SEQ}/{fn}", "timestamp_ns": t})
        pd.DataFrame(rows).set_index("timestamp_ns").to_parquet(root / "camera" / cam / f"{SEQ}.parquet")

    lidar_dir = root / "lidar" / "RefLidar" / SEQ
    lidar_dir.mkdir(parents=True)
    rows = []
    rng = np.random.default_rng(0)
    for i, t in enumerate(ts):
        fn = f"{i:06d}.pcd"
        pts = rng.random((10, 4), dtype=np.float32)
        PointCloud.from_xyzi_points(pts).save(lidar_dir / fn)
        rows.append({"filename": f"lidar/RefLidar/{SEQ}/{fn}", "timestamp_ns": t})
    pd.DataFrame(rows).set_index("timestamp_ns").to_parquet(root / "lidar" / "RefLidar" / f"{SEQ}.parquet")

    calib_dir = root / "calibration"
    (calib_dir / "sensor_extrinsics").mkdir(parents=True)
    (calib_dir / "camera_intrinsic").mkdir(parents=True)

    ext_rows = [
        {
            "sequence_id": NEAR_SEQ,
            "sensor_name": sensor,
            "tx": 3.13,
            "ty": 0.0,
            "tz": 1.3,
            "qx": 0.0,
            "qy": 0.0,
            "qz": 0.0,
            "qw": 1.0,
            "reference_point": "RearAxleCenterProjectedToGround",
        }
        for sensor in ["FC1", "TVfront", "RefLidar"]
    ]
    pd.DataFrame(ext_rows).set_index(["sequence_id", "sensor_name"]).to_parquet(
        calib_dir / "sensor_extrinsics" / "sensor_extrinsics.parquet"
    )

    for cam in ["FC1", "TVfront"]:
        pd.DataFrame(
            [
                {
                    "sequence_id": NEAR_SEQ,
                    "fx": 800.0,
                    "fy": 800.0,
                    "cx": 1000.0,
                    "cy": 750.0,
                    "width": 2000,
                    "height": 1200,
                    "cut_angle_lower": None,
                    "cut_angle_upper": None,
                }
            ]
        ).set_index("sequence_id").to_parquet(calib_dir / "camera_intrinsic" / f"{cam}.parquet")

    pd.DataFrame(
        [{"sequence_id": NEAR_SEQ, "vin": VIN, "vehicle_model": "ID. Buzz-1"}]
    ).set_index("sequence_id").to_parquet(root / "metadata.parquet")


class TestFolderToMcap(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.input_dir = self.tmpdir / "input"
        self.output_path = self.tmpdir / "out.mcap"
        _build_fixture(self.input_dir, num_frames=3)

    def test_convert_writes_expected_channels_and_counts(self):
        convert(self.input_dir, self.output_path)

        self.assertTrue(self.output_path.exists())
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            summary = reader.get_summary()
            topics = {ch.topic for ch in summary.channels.values()}
            self.assertEqual(
                topics,
                {
                    "/tf_static",
                    "/tf",
                    "/camera/FC1/image",
                    "/camera/FC1/intrinsics",
                    "/camera/TVfront/image",
                    "/camera/TVfront/intrinsics",
                    "/lidar/RefLidar/points",
                    "/metadata",
                },
            )
            counts = {
                ch.topic: summary.statistics.channel_message_counts[ch_id]
                for ch_id, ch in summary.channels.items()
            }
            self.assertEqual(counts["/camera/FC1/image"], 3)
            self.assertEqual(counts["/camera/TVfront/image"], 3)
            self.assertEqual(counts["/lidar/RefLidar/points"], 3)
            self.assertEqual(counts["/tf"], 3)
            self.assertEqual(counts["/tf_static"], 3)  # one per sensor
            self.assertEqual(counts["/camera/FC1/intrinsics"], 1)
            self.assertEqual(counts["/metadata"], 1)

    def test_falls_back_to_nearest_same_vin_calibration(self):
        # The target sequence_id is deliberately absent from the calibration
        # tables; conversion should still succeed using NEAR_SEQ's values.
        convert(self.input_dir, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            for _schema, channel, message in reader.iter_messages(topics=["/tf_static"]):
                obj = json.loads(message.data)
                self.assertEqual(obj["translation"]["x"], 3.13)

    def test_point_cloud_round_trips(self):
        convert(self.input_dir, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            _schema, _channel, message = next(
                reader.iter_messages(topics=["/lidar/RefLidar/points"])
            )
            obj = json.loads(message.data)
            raw = base64.b64decode(obj["data"])
            self.assertEqual(len(raw), 10 * 16)  # 10 points * (x,y,z,intensity float32)

    def test_camera_image_round_trips_as_valid_jpeg(self):
        convert(self.input_dir, self.output_path)
        with open(self.output_path, "rb") as f:
            reader = make_reader(f)
            _schema, _channel, message = next(
                reader.iter_messages(topics=["/camera/FC1/image"])
            )
            obj = json.loads(message.data)
            img_bytes = base64.b64decode(obj["data"])
            self.assertEqual(img_bytes[:2], b"\xff\xd8")  # JPEG magic bytes


if __name__ == "__main__":
    unittest.main()
