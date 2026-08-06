# folder_to_mcap

Converts a folder of camera/lidar/trajectory/calibration exports into a single
MCAP file for playback in Foxglove.

## Usage

```bash
pip install -r folder_to_mcap/requirements.txt
python -m folder_to_mcap.convert --input /path/to/one_sample_data --output recording.mcap
```

## Expected input layout

```
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
```

The `<SEQUENCE_ID>` is inferred from the single subfolder under `trajectory/`.

## Output topics

| Topic                          | Schema                                    | Notes |
|---------------------------------|--------------------------------------------|-------|
| `/tf_static`                    | `foxglove.FrameTransform`                   | one message per sensor, at the recording's start time |
| `/tf`                            | `foxglove.FrameTransform`                   | dynamic `map -> base_link` ego pose, one per trajectory row |
| `/camera/<CAM>/image`            | `foxglove.CompressedImage`                  | JPEG bytes, one per frame |
| `/camera/<CAM>/intrinsics`       | `foxglove_extension.CylindricalCameraInfo`  | published once; **not** a standard pinhole model, see below |
| `/lidar/<LIDAR>/points`          | `foxglove.PointCloud`                       | decoded from PCD (including `binary_compressed`) via `pypcd4`, repacked as `x,y,z,intensity` float32 |
| `/metadata`                      | `folder_to_mcap.RecordingMetadata`          | the recording's row from `metadata.parquet`, if present |

## Calibration fallback

If the target `SEQUENCE_ID` isn't present in `sensor_extrinsics.parquet`,
`camera_intrinsic/*.parquet`, or `metadata.parquet`, the converter falls back
to the nearest-dated recording from the same vehicle (matched by VIN prefix
of the sequence_id) and logs a warning. This is safe because sensor mounting
extrinsics and camera intrinsics are physical properties of the vehicle that
change only when it's recalibrated, not per-recording.

## Cylindrical camera model

These cameras use a cylindrical projection (`fx, fy, cx, cy` plus optional
horizontal cut angles), not a pinhole/plumb_bob model, so it doesn't fit
`foxglove.CameraCalibration`. Intrinsics are published as their own
`foxglove_extension.CylindricalCameraInfo` schema instead. The companion
Foxglove extension in `../foxglove-extension/` reads this schema to render
the images correctly in Foxglove Studio/desktop.
