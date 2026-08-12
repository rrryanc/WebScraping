#!/usr/bin/env python3
"""Convert a folder of camera/lidar/trajectory/calibration data into a single MCAP.

Expected input layout (as produced by the ad-alliance.biz style export this
tool was built against):

    <root>/
      metadata.parquet
      calibration/
        camera_intrinsic/<CAMERA>.parquet
        sensor_extrinsics/sensor_extrinsics.parquet
      camera/<CAMERA>/<SEQUENCE_ID>/*.jpg
      camera/<CAMERA>/<SEQUENCE_ID>.parquet      # filename -> timestamp_ns
      lidar/<LIDAR_NAME>/<SEQUENCE_ID>/*.pcd
      lidar/<LIDAR_NAME>/<SEQUENCE_ID>.parquet    # filename -> timestamp_ns
      trajectory/<SEQUENCE_ID>/trajectory.parquet
      visu/*.rrd                                  # ignored (pre-rendered Rerun recording)

Usage:
    python -m folder_to_mcap.convert --input /path/to/one_sample_data --output out.mcap
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd
from mcap.writer import CompressionType, Writer
from PIL import Image
from pypcd4 import PointCloud

from . import dewarp, schemas

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("folder_to_mcap")

SEQUENCE_DATE_RE = re.compile(r"^(?P<vin>[^_]+)_(?P<date>\d{8})_(?P<time>\d{6})")


def parse_sequence_id(sequence_id: str) -> tuple[str, str]:
    """Return (vin, "YYYYMMDDHHMMSS") parsed out of a sequence_id string."""
    m = SEQUENCE_DATE_RE.match(sequence_id)
    if not m:
        raise ValueError(f"Could not parse sequence_id: {sequence_id!r}")
    return m.group("vin"), m.group("date") + m.group("time")


def find_calibration_row(df: pd.DataFrame, target_sequence_id: str):
    """Return the row(s) for target_sequence_id, or the nearest-dated
    recording's row(s) for the same vehicle if the exact recording isn't
    present in the calibration table (extrinsics/intrinsics are physical-mount
    properties that change rarely). Works for both a plain sequence_id index
    (-> returns a Series) and a (sequence_id, sensor_name) MultiIndex
    (-> returns a DataFrame indexed by sensor_name).
    """
    is_multi = isinstance(df.index, pd.MultiIndex)
    seq_level = df.index.get_level_values(0) if is_multi else df.index

    def _select(seq_id: str):
        return df.xs(seq_id, level=0) if is_multi else df.loc[seq_id]

    if target_sequence_id in seq_level:
        return _select(target_sequence_id)

    target_vin, target_stamp = parse_sequence_id(target_sequence_id)
    candidates = []
    for seq_id in seq_level.unique():
        try:
            vin, stamp = parse_sequence_id(seq_id)
        except ValueError:
            continue
        if vin == target_vin:
            candidates.append((abs(int(stamp) - int(target_stamp)), seq_id))
    if not candidates:
        return None
    candidates.sort()
    nearest_seq_id = candidates[0][1]
    log.warning(
        "sequence_id %s not in calibration table; using nearest same-VIN "
        "recording %s as a stand-in",
        target_sequence_id,
        nearest_seq_id,
    )
    return _select(nearest_seq_id)


def read_frame_index(parquet_path: Path) -> pd.DataFrame:
    """Read a per-recording {filename, timestamp_ns} index, sorted by time."""
    df = pd.read_parquet(parquet_path)
    return df.sort_index()


class McapBuilder:
    def __init__(self, output_path: Path):
        self._f = open(output_path, "wb")
        self.writer = Writer(self._f, compression=CompressionType.ZSTD)
        self.writer.start(profile="", library="folder_to_mcap")
        self._schema_ids: dict[str, int] = {}
        self._channel_ids: dict[str, int] = {}

    def _schema_id(self, name: str, schema: dict) -> int:
        if name not in self._schema_ids:
            self._schema_ids[name] = self.writer.register_schema(
                name=name,
                encoding="jsonschema",
                data=json.dumps(schema).encode("utf-8"),
            )
        return self._schema_ids[name]

    def channel(self, topic: str, schema_name: str, schema: dict) -> int:
        if topic not in self._channel_ids:
            schema_id = self._schema_id(schema_name, schema)
            self._channel_ids[topic] = self.writer.register_channel(
                topic=topic, message_encoding="json", schema_id=schema_id
            )
        return self._channel_ids[topic]

    def write_json(self, topic: str, schema_name: str, schema: dict, timestamp_ns: int, obj: dict):
        channel_id = self.channel(topic, schema_name, schema)
        self.writer.add_message(
            channel_id=channel_id,
            log_time=timestamp_ns,
            publish_time=timestamp_ns,
            data=json.dumps(obj).encode("utf-8"),
        )

    def finish(self):
        self.writer.finish()
        self._f.close()


def _time_obj(timestamp_ns: int) -> dict:
    return {"sec": timestamp_ns // 1_000_000_000, "nsec": timestamp_ns % 1_000_000_000}


def _quat_mul(q1: tuple[float, float, float, float], q2: tuple[float, float, float, float]):
    """Hamilton product of two (x, y, z, w) quaternions: q1 applied first, q2
    applied in the frame that results from q1 (i.e. R = R1 @ R2)."""
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return (
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    )


# Extrinsics for this vehicle are given in a body-mount convention (child
# frame axes: X-forward, Y-left, Z-up). Image/CameraCalibration consumers
# (Foxglove's 3D panel included) instead expect the camera's frame to be in
# "optical" convention (X-right, Y-down, Z-forward -- i.e. images project
# along +Z). Composing this fixed rotation into a camera's static transform
# converts from the former to the latter; without it, every camera's image
# plane renders facing straight up (body Z) instead of out from the vehicle.
_OPTICAL_FRAME_ROTATION = (-0.5, 0.5, -0.5, 0.5)  # (x, y, z, w)


def write_static_transforms(
    builder: McapBuilder,
    extrinsics: pd.DataFrame,
    base_frame: str,
    t0: int,
    camera_names: set[str] = frozenset(),
):
    for sensor_name, row in extrinsics.iterrows():
        parent = str(row.get("reference_point") or base_frame)
        qx, qy, qz, qw = float(row["qx"]), float(row["qy"]), float(row["qz"]), float(row["qw"])
        if sensor_name in camera_names:
            qx, qy, qz, qw = _quat_mul((qx, qy, qz, qw), _OPTICAL_FRAME_ROTATION)
        builder.write_json(
            "/tf_static",
            "foxglove.FrameTransform",
            schemas.FOXGLOVE_FRAME_TRANSFORM,
            t0,
            {
                "timestamp": _time_obj(t0),
                "parent_frame_id": base_frame,
                "child_frame_id": sensor_name,
                "translation": {"x": float(row["tx"]), "y": float(row["ty"]), "z": float(row["tz"])},
                "rotation": {"x": qx, "y": qy, "z": qz, "w": qw},
            },
        )
        log.info("wrote static transform %s -> %s (parent recorded as %r)", base_frame, sensor_name, parent)


def write_camera_intrinsics(builder: McapBuilder, camera: str, row: pd.Series, t0: int):
    builder.write_json(
        f"/camera/{camera}/intrinsics",
        "foxglove_extension.CylindricalCameraInfo",
        schemas.CYLINDRICAL_CAMERA_INFO,
        t0,
        {
            "frame_id": camera,
            "projection": "cylinder",
            "fx": float(row["fx"]),
            "fy": float(row["fy"]),
            "cx": float(row["cx"]),
            "cy": float(row["cy"]),
            "width": int(row["width"]),
            "height": int(row["height"]),
            "cut_angle_lower": None if pd.isna(row.get("cut_angle_lower")) else float(row["cut_angle_lower"]),
            "cut_angle_upper": None if pd.isna(row.get("cut_angle_upper")) else float(row["cut_angle_upper"]),
        },
    )


def write_camera_calibration(
    builder: McapBuilder, camera: str, width: int, height: int, f_virtual: float, t0: int
):
    """Write a foxglove.CameraCalibration message describing the virtual
    pinhole camera that /camera/{camera}/image_rect was rectified into, so
    Foxglove's built-in 3D panel can project the rectified image directly."""
    cx = width / 2
    cy = height / 2
    builder.write_json(
        f"/camera/{camera}/calibration",
        "foxglove.CameraCalibration",
        schemas.FOXGLOVE_CAMERA_CALIBRATION,
        t0,
        {
            "timestamp": _time_obj(t0),
            "frame_id": camera,
            "width": width,
            "height": height,
            "distortion_model": "",
            "D": [],
            "K": [f_virtual, 0.0, cx, 0.0, f_virtual, cy, 0.0, 0.0, 1.0],
            "R": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            "P": [f_virtual, 0.0, cx, 0.0, 0.0, f_virtual, cy, 0.0, 0.0, 0.0, 1.0, 0.0],
        },
    )


def write_camera_frames(
    builder: McapBuilder,
    camera: str,
    camera_dir: Path,
    index_parquet: Path,
    intrinsics_row: pd.Series | None,
    horizontal_fov_deg: float,
) -> tuple[int, int, float] | None:
    """Write raw /camera/{camera}/image frames, and, if intrinsics_row is
    given, also write dewarped /camera/{camera}/image_rect frames (built from
    a single remap LUT reused across all frames). Returns the
    (dest_width, dest_height, f_virtual) of the rectified view, or None if no
    rectification was performed."""
    frame_index = read_frame_index(index_parquet)
    topic = f"/camera/{camera}/image"
    rect_topic = f"/camera/{camera}/image_rect"

    lut = None
    dest_width = dest_height = None
    if intrinsics_row is not None:
        src_width = int(intrinsics_row["width"])
        src_height = int(intrinsics_row["height"])
        dest_width, dest_height = src_width, src_height
        lut = dewarp.build_remap_lut(
            fx=float(intrinsics_row["fx"]),
            fy=float(intrinsics_row["fy"]),
            cx=float(intrinsics_row["cx"]),
            cy=float(intrinsics_row["cy"]),
            src_width=src_width,
            src_height=src_height,
            dest_width=dest_width,
            dest_height=dest_height,
            horizontal_fov_deg=horizontal_fov_deg,
        )

    count = 0
    for timestamp_ns, row in frame_index.iterrows():
        filename = Path(row["filename"]).name
        jpg_path = camera_dir / filename
        if not jpg_path.exists():
            log.warning("missing camera frame %s", jpg_path)
            continue
        data = jpg_path.read_bytes()
        builder.write_json(
            topic,
            "foxglove.CompressedImage",
            schemas.FOXGLOVE_COMPRESSED_IMAGE,
            int(timestamp_ns),
            {
                "timestamp": _time_obj(int(timestamp_ns)),
                "frame_id": camera,
                "format": "jpeg",
                "data": base64.b64encode(data).decode("ascii"),
            },
        )
        if lut is not None:
            with Image.open(io.BytesIO(data)) as im:
                src_rgb = np.array(im.convert("RGB"))
            rect_rgb = dewarp.apply_remap(lut, src_rgb)
            buf = io.BytesIO()
            Image.fromarray(rect_rgb).save(buf, format="JPEG", quality=90)
            builder.write_json(
                rect_topic,
                "foxglove.CompressedImage",
                schemas.FOXGLOVE_COMPRESSED_IMAGE,
                int(timestamp_ns),
                {
                    "timestamp": _time_obj(int(timestamp_ns)),
                    "frame_id": camera,
                    "format": "jpeg",
                    "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                },
            )
        count += 1
    log.info(
        "wrote %d frames for camera %s%s", count, camera, " (+ rectified)" if lut is not None else ""
    )
    return (dest_width, dest_height, lut.f_virtual) if lut is not None else None


def _pack_point_cloud_xyzi(points_xyzi: np.ndarray) -> bytes:
    """Pack an (N, 4) float array of x,y,z,intensity into foxglove.PointCloud
    binary data: 4 float32 fields per point, 16-byte stride."""
    return points_xyzi.astype("<f4").tobytes()


def write_lidar_frames(builder: McapBuilder, lidar: str, lidar_dir: Path, index_parquet: Path):
    frame_index = read_frame_index(index_parquet)
    topic = f"/lidar/{lidar}/points"
    fields = [
        {"name": "x", "offset": 0, "type": schemas.POINT_FIELD_FLOAT32},
        {"name": "y", "offset": 4, "type": schemas.POINT_FIELD_FLOAT32},
        {"name": "z", "offset": 8, "type": schemas.POINT_FIELD_FLOAT32},
        {"name": "intensity", "offset": 12, "type": schemas.POINT_FIELD_FLOAT32},
    ]
    count = 0
    for timestamp_ns, row in frame_index.iterrows():
        filename = Path(row["filename"]).name
        pcd_path = lidar_dir / filename
        if not pcd_path.exists():
            log.warning("missing lidar frame %s", pcd_path)
            continue
        pc = PointCloud.from_path(pcd_path)
        arr = pc.numpy(("x", "y", "z", "intensity"))
        data_bytes = _pack_point_cloud_xyzi(arr)
        builder.write_json(
            topic,
            "foxglove.PointCloud",
            schemas.FOXGLOVE_POINT_CLOUD,
            int(timestamp_ns),
            {
                "timestamp": _time_obj(int(timestamp_ns)),
                "frame_id": lidar,
                "pose": {
                    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
                "point_stride": 16,
                "fields": fields,
                "data": base64.b64encode(data_bytes).decode("ascii"),
            },
        )
        count += 1
    log.info("wrote %d frames for lidar %s", count, lidar)


def write_trajectory(builder: McapBuilder, trajectory_parquet: Path, map_frame: str, base_frame: str):
    df = pd.read_parquet(trajectory_parquet).sort_index()
    for timestamp_ns, row in df.iterrows():
        builder.write_json(
            "/tf",
            "foxglove.FrameTransform",
            schemas.FOXGLOVE_FRAME_TRANSFORM,
            int(timestamp_ns),
            {
                "timestamp": _time_obj(int(timestamp_ns)),
                "parent_frame_id": map_frame,
                "child_frame_id": base_frame,
                "translation": {"x": float(row["tx"]), "y": float(row["ty"]), "z": float(row["tz"])},
                "rotation": {
                    "x": float(row["qx"]),
                    "y": float(row["qy"]),
                    "z": float(row["qz"]),
                    "w": float(row["qw"]),
                },
            },
        )
    log.info("wrote %d trajectory poses", len(df))


def write_trajectory_path(builder: McapBuilder, trajectory_parquet: Path, map_frame: str, t0: int):
    """Write the whole trajectory as a single foxglove.PosesInFrame message so
    Foxglove's 3D panel can render it as a static path/trail, independent of
    playback position (unlike /tf, which only carries the current pose)."""
    df = pd.read_parquet(trajectory_parquet).sort_index()
    poses = [
        {
            "position": {"x": float(row["tx"]), "y": float(row["ty"]), "z": float(row["tz"])},
            "orientation": {
                "x": float(row["qx"]),
                "y": float(row["qy"]),
                "z": float(row["qz"]),
                "w": float(row["qw"]),
            },
        }
        for _, row in df.iterrows()
    ]
    builder.write_json(
        "/trajectory/path",
        "foxglove.PosesInFrame",
        schemas.FOXGLOVE_POSES_IN_FRAME,
        t0,
        {"timestamp": _time_obj(t0), "frame_id": map_frame, "poses": poses},
    )
    log.info("wrote trajectory path with %d poses", len(poses))


def convert(
    input_dir: Path,
    output_path: Path,
    base_frame: str = "base_link",
    map_frame: str = "map",
    rectify_fov_deg: float = 90.0,
):
    trajectory_root = input_dir / "trajectory"
    sequence_dirs = [p for p in trajectory_root.iterdir() if p.is_dir()] if trajectory_root.exists() else []
    if len(sequence_dirs) != 1:
        raise RuntimeError(
            f"Expected exactly one sequence under {trajectory_root}, found {len(sequence_dirs)}"
        )
    sequence_id = sequence_dirs[0].name
    log.info("sequence_id: %s", sequence_id)

    trajectory_parquet = sequence_dirs[0] / "trajectory.parquet"
    if not trajectory_parquet.exists():
        raise RuntimeError(f"trajectory.parquet not found at {trajectory_parquet}")

    # Determine the earliest timestamp across all data, used for static
    # (one-shot) messages: /tf_static and per-camera intrinsics.
    traj_df = pd.read_parquet(trajectory_parquet)
    t0 = int(traj_df.index.min())

    camera_root = input_dir / "camera"
    camera_names = (
        {p.name for p in camera_root.iterdir() if p.is_dir()} if camera_root.exists() else set()
    )

    builder = McapBuilder(output_path)
    try:
        # --- calibration -----------------------------------------------
        extrinsics_path = input_dir / "calibration" / "sensor_extrinsics" / "sensor_extrinsics.parquet"
        extrinsics_row = None
        if extrinsics_path.exists():
            extrinsics_df = pd.read_parquet(extrinsics_path)
            per_sensor = find_calibration_row(extrinsics_df, sequence_id)
            if per_sensor is not None:
                write_static_transforms(builder, per_sensor, base_frame, t0, camera_names)

        intrinsics_dir = input_dir / "calibration" / "camera_intrinsic"
        intrinsics_rows: dict[str, pd.Series] = {}
        if intrinsics_dir.exists():
            for intr_path in sorted(intrinsics_dir.glob("*.parquet")):
                camera = intr_path.stem
                df = pd.read_parquet(intr_path)
                row = find_calibration_row(df, sequence_id)
                if row is not None:
                    intrinsics_rows[camera] = row
                    write_camera_intrinsics(builder, camera, row, t0)

        # --- cameras -----------------------------------------------------
        if camera_root.exists():
            for camera_dir in sorted(camera_root.iterdir()):
                if not camera_dir.is_dir():
                    continue
                camera = camera_dir.name
                seq_dir = camera_dir / sequence_id
                index_parquet = camera_dir / f"{sequence_id}.parquet"
                if not seq_dir.exists() or not index_parquet.exists():
                    log.warning("skipping camera %s: missing %s or %s", camera, seq_dir, index_parquet)
                    continue
                rectified = write_camera_frames(
                    builder, camera, seq_dir, index_parquet, intrinsics_rows.get(camera), rectify_fov_deg
                )
                if rectified is not None:
                    dest_width, dest_height, f_virtual = rectified
                    write_camera_calibration(builder, camera, dest_width, dest_height, f_virtual, t0)

        # --- lidar ---------------------------------------------------------
        lidar_root = input_dir / "lidar"
        if lidar_root.exists():
            for lidar_dir in sorted(lidar_root.iterdir()):
                if not lidar_dir.is_dir():
                    continue
                lidar = lidar_dir.name
                seq_dir = lidar_dir / sequence_id
                index_parquet = lidar_dir / f"{sequence_id}.parquet"
                if not seq_dir.exists() or not index_parquet.exists():
                    log.warning("skipping lidar %s: missing %s or %s", lidar, seq_dir, index_parquet)
                    continue
                write_lidar_frames(builder, lidar, seq_dir, index_parquet)

        # --- trajectory ------------------------------------------------
        write_trajectory(builder, trajectory_parquet, map_frame, base_frame)
        write_trajectory_path(builder, trajectory_parquet, map_frame, t0)

        # --- recording metadata -----------------------------------------
        metadata_path = input_dir / "metadata.parquet"
        if metadata_path.exists():
            meta_df = pd.read_parquet(metadata_path)
            row = find_calibration_row(meta_df, sequence_id)
            if row is not None:
                obj = {k: (None if pd.isna(v) else str(v)) for k, v in row.items()}
                obj["sequence_id"] = sequence_id
                builder.write_json(
                    "/metadata", "folder_to_mcap.RecordingMetadata", schemas.RECORDING_METADATA, t0, obj
                )
    finally:
        builder.finish()

    log.info("wrote %s", output_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="Path to the folder (e.g. one_sample_data/)")
    parser.add_argument("--output", required=True, type=Path, help="Path to write the .mcap file")
    parser.add_argument("--base-frame", default="base_link")
    parser.add_argument("--map-frame", default="map")
    parser.add_argument(
        "--rectify-fov",
        type=float,
        default=90.0,
        help="Horizontal FOV (degrees) of the virtual pinhole camera used to dewarp each "
        "cylindrical camera's images into /camera/{camera}/image_rect (default: 90.0)",
    )
    args = parser.parse_args()
    convert(args.input, args.output, args.base_frame, args.map_frame, args.rectify_fov)


if __name__ == "__main__":
    main()
