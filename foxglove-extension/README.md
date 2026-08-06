# Cylindrical Camera View

A Foxglove extension panel for cameras that use a cylindrical projection
model (`fx, fy, cx, cy` plus optional cut angles) instead of a standard
pinhole/plumb_bob model — the kind produced by `folder_to_mcap` in this
repository on the `foxglove_extension.CylindricalCameraInfo` schema.

Foxglove's built-in Image panel assumes `foxglove.CameraCalibration`'s
pinhole distortion model, so it can't correctly undistort these images. This
panel instead:

1. Subscribes to a `foxglove.CompressedImage` topic and a
   `foxglove_extension.CylindricalCameraInfo` topic.
2. Builds a remap lookup table that inverts the cylindrical projection for a
   chosen virtual pinhole field of view.
3. Dewarps each incoming frame into a normal-looking perspective image and
   draws it to a canvas.

## Development

```bash
npm install
npm run build     # type-check + bundle
npm run local-install   # install into your local Foxglove app for testing
```

## Packaging

```bash
npm run package   # produces a .foxe file you can share or upload
```

## Usage

Add the "Cylindrical Camera View" panel to a Foxglove layout, then pick the
image topic and the matching intrinsics topic in the panel's toolbar (both
default to the first matching topic found, if any). Adjust "Horizontal FOV"
to trade off how much of the cylindrical field of view is shown vs.
perspective distortion at the edges.
