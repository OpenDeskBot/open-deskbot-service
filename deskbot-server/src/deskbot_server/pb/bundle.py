"""face_bundle 公共 API。"""

from deskbot_server.pb.face_bundle import (
    default_pb_face_bundle,
    demo_pb_face_bundle,
    ensure_pb_face_bundle_shape,
    hot_reload_pb_face_bundle_file,
    load_pb_face_bundle_file,
    load_pb_face_bundle_json_document,
    merge_pb_face_bundle,
    resolve_pb_face_bundle,
)

__all__ = [
    "default_pb_face_bundle",
    "demo_pb_face_bundle",
    "ensure_pb_face_bundle_shape",
    "hot_reload_pb_face_bundle_file",
    "load_pb_face_bundle_file",
    "load_pb_face_bundle_json_document",
    "merge_pb_face_bundle",
    "resolve_pb_face_bundle",
]
