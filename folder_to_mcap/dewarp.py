"""Python port of the cylindrical-camera dewarp math used by the companion
Foxglove extension (foxglove-extension/src/dewarp.ts).

The converter uses this to also emit a rectified (virtual pinhole) image
alongside each camera's raw cylindrical frames, plus a matching
foxglove.CameraCalibration message, so Foxglove's built-in 3D panel can
project the images directly (it only understands pinhole/plumb_bob
calibration, not this vehicle's cylindrical projection).

    theta = atan2(X, Z)
    r     = sqrt(X*X + Z*Z)
    u     = fx * theta + cx
    v     = fy * (Y / r) + cy

for a 3D point (X, Y, Z) in the camera frame (Z forward). To dewarp, for
every destination pixel we compute the camera-frame ray a virtual pinhole
camera would have produced, then invert the projection above to find the
corresponding source pixel to sample.
"""

from __future__ import annotations

import math

import numpy as np


class RemapLut:
    def __init__(self, u: np.ndarray, v: np.ndarray, valid: np.ndarray, f_virtual: float, dest_width: int, dest_height: int):
        self.u = u
        self.v = v
        self.valid = valid
        self.f_virtual = f_virtual
        self.dest_width = dest_width
        self.dest_height = dest_height


def build_remap_lut(
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    src_width: int,
    src_height: int,
    dest_width: int,
    dest_height: int,
    horizontal_fov_deg: float,
) -> RemapLut:
    """Build a remap LUT that dewarps a cylindrical source image (described by
    fx/fy/cx/cy/src_width/src_height) into a dest_width x dest_height
    pinhole-equivalent perspective view with the given horizontal FOV."""
    half_fov = math.radians(horizontal_fov_deg) / 2
    # Virtual pinhole focal length chosen so the requested horizontal FOV maps
    # across dest_width pixels: tan(halfFov) = (destWidth/2) / fVirtual.
    f_virtual = dest_width / 2 / math.tan(half_fov)
    dest_cx = dest_width / 2
    dest_cy = dest_height / 2

    dx, dy = np.meshgrid(
        np.arange(dest_width, dtype=np.float64), np.arange(dest_height, dtype=np.float64)
    )
    X = (dx - dest_cx) / f_virtual
    Y = (dy - dest_cy) / f_virtual
    Z = 1.0

    theta = np.arctan2(X, Z)
    r = np.sqrt(X * X + Z * Z)

    u = fx * theta + cx
    v = fy * Y / r + cy

    valid = (u >= 0) & (u < src_width) & (v >= 0) & (v < src_height)
    return RemapLut(u, v, valid, f_virtual, dest_width, dest_height)


def apply_remap(lut: RemapLut, src_rgb: np.ndarray) -> np.ndarray:
    """Bilinearly resample src_rgb (H, W, 3 uint8) per `lut`, returning a
    (dest_height, dest_width, 3 uint8) array. Pixels the LUT marked
    out-of-bounds are filled black."""
    src_h, src_w = src_rgb.shape[:2]
    u, v = lut.u, lut.v

    u0 = np.floor(u)
    v0 = np.floor(v)
    fu = (u - u0).astype(np.float32)[..., None]
    fv = (v - v0).astype(np.float32)[..., None]

    u0c = np.clip(u0, 0, src_w - 1).astype(np.int64)
    v0c = np.clip(v0, 0, src_h - 1).astype(np.int64)
    u1c = np.clip(u0 + 1, 0, src_w - 1).astype(np.int64)
    v1c = np.clip(v0 + 1, 0, src_h - 1).astype(np.int64)

    p00 = src_rgb[v0c, u0c].astype(np.float32)
    p10 = src_rgb[v0c, u1c].astype(np.float32)
    p01 = src_rgb[v1c, u0c].astype(np.float32)
    p11 = src_rgb[v1c, u1c].astype(np.float32)

    top = p00 * (1 - fu) + p10 * fu
    bottom = p01 * (1 - fu) + p11 * fu
    out = top * (1 - fv) + bottom * fv
    out = np.clip(out, 0, 255).astype(np.uint8)
    out[~lut.valid] = 0
    return out
