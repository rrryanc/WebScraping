"""JSON Schema definitions used when writing MCAP channels.

The foxglove.* schemas mirror the well-known schemas from
https://github.com/foxglove/schemas so that Foxglove Studio/desktop
auto-detects the right visualization for each channel. Everything is
written with the "json" message encoding (schema encoding "jsonschema"),
so no protobuf/flatbuffer toolchain is required to produce the MCAP.
"""

_TIME = {
    "type": "object",
    "properties": {
        "sec": {"type": "integer"},
        "nsec": {"type": "integer"},
    },
}

_VECTOR3 = {
    "type": "object",
    "properties": {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "z": {"type": "number"},
    },
}

_QUATERNION = {
    "type": "object",
    "properties": {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "z": {"type": "number"},
        "w": {"type": "number"},
    },
}

FOXGLOVE_COMPRESSED_IMAGE = {
    "title": "foxglove.CompressedImage",
    "type": "object",
    "properties": {
        "timestamp": _TIME,
        "frame_id": {"type": "string"},
        "data": {"type": "string", "contentEncoding": "base64"},
        "format": {"type": "string"},
    },
}

FOXGLOVE_POINT_CLOUD = {
    "title": "foxglove.PointCloud",
    "type": "object",
    "properties": {
        "timestamp": _TIME,
        "frame_id": {"type": "string"},
        "pose": {
            "type": "object",
            "properties": {
                "position": _VECTOR3,
                "orientation": _QUATERNION,
            },
        },
        "point_stride": {"type": "integer"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "offset": {"type": "integer"},
                    "type": {"type": "integer"},
                },
            },
        },
        "data": {"type": "string", "contentEncoding": "base64"},
    },
}

FOXGLOVE_FRAME_TRANSFORM = {
    "title": "foxglove.FrameTransform",
    "type": "object",
    "properties": {
        "timestamp": _TIME,
        "parent_frame_id": {"type": "string"},
        "child_frame_id": {"type": "string"},
        "translation": _VECTOR3,
        "rotation": _QUATERNION,
    },
}

FOXGLOVE_POSES_IN_FRAME = {
    "title": "foxglove.PosesInFrame",
    "type": "object",
    "properties": {
        "timestamp": _TIME,
        "frame_id": {"type": "string"},
        "poses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "position": _VECTOR3,
                    "orientation": _QUATERNION,
                },
            },
        },
    },
}

# PackedElementField.NumericType values used by foxglove.PointCloud.
POINT_FIELD_FLOAT32 = 7

# Not a standard foxglove schema: this vehicle's cameras use a cylindrical
# projection model (fx/fy/cx/cy plus horizontal/vertical cut angles), which
# does not fit foxglove.CameraCalibration's pinhole/plumb_bob assumptions.
# Published as its own well-named schema so the companion Foxglove
# extension (see foxglove-extension/) can pick it up directly.
CYLINDRICAL_CAMERA_INFO = {
    "title": "foxglove_extension.CylindricalCameraInfo",
    "type": "object",
    "properties": {
        "frame_id": {"type": "string"},
        "projection": {"type": "string", "const": "cylinder"},
        "fx": {"type": "number"},
        "fy": {"type": "number"},
        "cx": {"type": "number"},
        "cy": {"type": "number"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "cut_angle_lower": {"type": ["number", "null"]},
        "cut_angle_upper": {"type": ["number", "null"]},
    },
}

FOXGLOVE_CAMERA_CALIBRATION = {
    "title": "foxglove.CameraCalibration",
    "type": "object",
    "properties": {
        "timestamp": _TIME,
        "frame_id": {"type": "string"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "distortion_model": {"type": "string"},
        "D": {"type": "array", "items": {"type": "number"}},
        "K": {"type": "array", "items": {"type": "number"}},
        "R": {"type": "array", "items": {"type": "number"}},
        "P": {"type": "array", "items": {"type": "number"}},
    },
}

RECORDING_METADATA = {
    "title": "folder_to_mcap.RecordingMetadata",
    "type": "object",
}
