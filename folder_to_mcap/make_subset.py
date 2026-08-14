#!/usr/bin/env python3
"""Create a small subset of a folder_to_mcap input directory, for fast local
iteration (the full ~450-frame/8-camera conversion is slow, especially with
rectification enabled).

Keeps only the first --num-frames trajectory poses, plus whichever
camera/lidar frames fall within that time range, and only the cameras listed
in --cameras (default: all cameras found). Calibration and metadata are
copied in full (they're tiny). Camera/lidar frame files are symlinked by
default (fast, no extra disk space) -- pass --copy if your filesystem
doesn't support symlinks.

Usage:
    python -m folder_to_mcap.make_subset --input /path/to/full_data \
        --output /path/to/subset_data --num-frames 30 --cameras FC1,TVright

    python -m folder_to_mcap.convert --input /path/to/subset_data --output subset.mcap
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd


def _find_sequence_dir(root: Path) -> Path:
    trajectory_root = root / "trajectory"
    sequence_dirs = [p for p in trajectory_root.iterdir() if p.is_dir()] if trajectory_root.exists() else []
    if len(sequence_dirs) != 1:
        raise RuntimeError(f"Expected exactly one sequence under {trajectory_root}, found {len(sequence_dirs)}")
    return sequence_dirs[0]


def _subset_frames(
    sensor_dir: Path,
    sequence_id: str,
    out_sensor_dir: Path,
    t_start: int,
    t_end: int,
    copy: bool,
):
    index_parquet = sensor_dir / f"{sequence_id}.parquet"
    frames_dir = sensor_dir / sequence_id
    if not index_parquet.exists() or not frames_dir.exists():
        print(f"skipping {sensor_dir.name}: missing {index_parquet} or {frames_dir}")
        return

    df = pd.read_parquet(index_parquet).sort_index()
    subset = df[(df.index >= t_start) & (df.index <= t_end)]

    out_sensor_dir.mkdir(parents=True, exist_ok=True)
    subset.to_parquet(out_sensor_dir / f"{sequence_id}.parquet")

    out_frames_dir = out_sensor_dir / sequence_id
    out_frames_dir.mkdir(parents=True, exist_ok=True)
    linked = 0
    for _, row in subset.iterrows():
        filename = Path(row["filename"]).name
        src_file = frames_dir / filename
        dst_file = out_frames_dir / filename
        if not src_file.exists():
            print(f"missing source frame {src_file}")
            continue
        if not dst_file.exists():
            if copy:
                shutil.copy2(src_file, dst_file)
            else:
                dst_file.symlink_to(src_file.resolve())
        linked += 1
    print(f"{sensor_dir.name}: kept {linked} frames")


def make_subset(
    input_dir: Path,
    output_dir: Path,
    num_frames: int,
    cameras: list[str] | None,
    copy: bool = False,
):
    sequence_dir = _find_sequence_dir(input_dir)
    sequence_id = sequence_dir.name

    trajectory_parquet = sequence_dir / "trajectory.parquet"
    traj_df = pd.read_parquet(trajectory_parquet).sort_index()
    subset_traj = traj_df.iloc[:num_frames]
    if subset_traj.empty:
        raise RuntimeError("num_frames produced an empty trajectory subset")
    t_start, t_end = int(subset_traj.index.min()), int(subset_traj.index.max())
    print(
        f"sequence_id={sequence_id}, keeping {len(subset_traj)} trajectory poses, "
        f"time range [{t_start}, {t_end}]"
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    # metadata + calibration are tiny -- copy in full.
    for name in ["metadata.parquet", "calibration"]:
        src = input_dir / name
        if not src.exists():
            continue
        dst = output_dir / name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    out_traj_dir = output_dir / "trajectory" / sequence_id
    out_traj_dir.mkdir(parents=True, exist_ok=True)
    subset_traj.to_parquet(out_traj_dir / "trajectory.parquet")

    camera_root = input_dir / "camera"
    if camera_root.exists():
        available = sorted(p.name for p in camera_root.iterdir() if p.is_dir())
        selected = cameras if cameras is not None else available
        unknown = sorted(set(selected) - set(available))
        if unknown:
            raise RuntimeError(f"Unknown camera(s) {unknown}; available cameras: {available}")
        for camera in selected:
            _subset_frames(
                camera_root / camera, sequence_id, output_dir / "camera" / camera, t_start, t_end, copy
            )

    lidar_root = input_dir / "lidar"
    if lidar_root.exists():
        for lidar_dir in sorted(p for p in lidar_root.iterdir() if p.is_dir()):
            _subset_frames(
                lidar_dir, sequence_id, output_dir / "lidar" / lidar_dir.name, t_start, t_end, copy
            )

    print(f"wrote subset to {output_dir}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="Full input folder (e.g. one_sample_data/)")
    parser.add_argument("--output", required=True, type=Path, help="Path to write the subset folder")
    parser.add_argument(
        "--num-frames", type=int, default=30, help="Number of trajectory poses to keep (default: 30)"
    )
    parser.add_argument(
        "--cameras",
        type=str,
        default=None,
        help="Comma-separated camera names to keep, e.g. FC1,TVright (default: all cameras found)",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy frame files instead of symlinking (use if your filesystem doesn't support symlinks)",
    )
    args = parser.parse_args()
    cameras = [c.strip() for c in args.cameras.split(",")] if args.cameras else None
    make_subset(args.input, args.output, args.num_frames, cameras, args.copy)


if __name__ == "__main__":
    main()
