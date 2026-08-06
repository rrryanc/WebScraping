/**
 * Cylindrical camera model used by these vehicles' cameras:
 *
 *   theta = atan2(X, Z)                 // horizontal angle from optical axis
 *   r     = sqrt(X*X + Z*Z)
 *   u     = fx * theta + cx
 *   v     = fy * (Y / r) + cy
 *
 * for a 3D point (X, Y, Z) in the camera frame (Z forward). This is not a
 * pinhole/plumb_bob model, so foxglove.CameraCalibration's distortion
 * pipeline doesn't apply. To display these images undistorted, we build a
 * remap lookup table: for every destination (rectified, pinhole) pixel we
 * compute the camera-frame ray a pinhole camera with virtual focal length
 * `fVirtual` would have produced, then invert the cylindrical projection
 * above to find the corresponding source pixel.
 */

export interface CylindricalIntrinsics {
  fx: number;
  fy: number;
  cx: number;
  cy: number;
  width: number;
  height: number;
}

export interface RemapLut {
  width: number;
  height: number;
  // For each destination pixel i, srcX[i]/srcY[i] hold the source pixel
  // coordinates to sample (bilinearly). NaN entries are out of bounds.
  srcX: Float32Array;
  srcY: Float32Array;
}

/**
 * Build a remap LUT that dewarps `intrinsics`-shaped cylindrical source
 * images into a `destWidth`x`destHeight` pinhole-equivalent perspective view
 * with the given horizontal field of view (degrees).
 */
export function buildRemapLut(
  intrinsics: CylindricalIntrinsics,
  destWidth: number,
  destHeight: number,
  horizontalFovDeg: number,
): RemapLut {
  const srcX = new Float32Array(destWidth * destHeight);
  const srcY = new Float32Array(destWidth * destHeight);

  const halfFovRad = (horizontalFovDeg * Math.PI) / 180 / 2;
  // Virtual pinhole focal length chosen so the requested horizontal FOV maps
  // across destWidth pixels: tan(halfFov) = (destWidth/2) / fVirtual.
  const fVirtual = destWidth / 2 / Math.tan(halfFovRad);
  const destCx = destWidth / 2;
  const destCy = destHeight / 2;

  for (let dy = 0; dy < destHeight; dy++) {
    for (let dx = 0; dx < destWidth; dx++) {
      const X = (dx - destCx) / fVirtual;
      const Y = (dy - destCy) / fVirtual;
      const Z = 1;

      const theta = Math.atan2(X, Z);
      const r = Math.sqrt(X * X + Z * Z);

      const u = intrinsics.fx * theta + intrinsics.cx;
      const v = (intrinsics.fy * Y) / r + intrinsics.cy;

      const idx = dy * destWidth + dx;
      if (u >= 0 && u < intrinsics.width && v >= 0 && v < intrinsics.height) {
        srcX[idx] = u;
        srcY[idx] = v;
      } else {
        srcX[idx] = NaN;
        srcY[idx] = NaN;
      }
    }
  }

  return { width: destWidth, height: destHeight, srcX, srcY };
}

/** Bilinearly sample `src` (RGBA, width x height) at floating point (x, y). */
function sampleBilinear(
  src: Uint8ClampedArray,
  srcWidth: number,
  srcHeight: number,
  x: number,
  y: number,
  out: [number, number, number, number],
): void {
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const x1 = Math.min(x0 + 1, srcWidth - 1);
  const y1 = Math.min(y0 + 1, srcHeight - 1);
  const fx = x - x0;
  const fy = y - y0;

  for (let c = 0; c < 4; c++) {
    const p00 = src[(y0 * srcWidth + x0) * 4 + c] ?? 0;
    const p10 = src[(y0 * srcWidth + x1) * 4 + c] ?? 0;
    const p01 = src[(y1 * srcWidth + x0) * 4 + c] ?? 0;
    const p11 = src[(y1 * srcWidth + x1) * 4 + c] ?? 0;
    const top = p00 * (1 - fx) + p10 * fx;
    const bottom = p01 * (1 - fx) + p11 * fx;
    out[c] = top * (1 - fy) + bottom * fy;
  }
}

/** Apply a remap LUT to a decoded source image, producing a dewarped ImageData. */
export function applyRemap(
  lut: RemapLut,
  srcImageData: ImageData,
): ImageData {
  const dest = new ImageData(lut.width, lut.height);
  const pixel: [number, number, number, number] = [0, 0, 0, 0];

  for (let i = 0; i < lut.width * lut.height; i++) {
    const sx = lut.srcX[i];
    const sy = lut.srcY[i];
    if (sx === undefined || sy === undefined || Number.isNaN(sx) || Number.isNaN(sy)) {
      dest.data[i * 4 + 3] = 0; // transparent / out of frame
      continue;
    }
    sampleBilinear(srcImageData.data, srcImageData.width, srcImageData.height, sx, sy, pixel);
    dest.data[i * 4 + 0] = pixel[0];
    dest.data[i * 4 + 1] = pixel[1];
    dest.data[i * 4 + 2] = pixel[2];
    dest.data[i * 4 + 3] = 255;
  }

  return dest;
}
