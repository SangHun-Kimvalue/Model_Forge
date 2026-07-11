"""Lightweight STL preview renderer for local artifact evidence."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

trimesh: Any | None
_TRIMESH_IMPORT_ERROR: ImportError | None
try:
    import trimesh
except ImportError as exc:  # pragma: no cover - dependency guard.
    trimesh = None
    _TRIMESH_IMPORT_ERROR = exc
else:
    _TRIMESH_IMPORT_ERROR = None


def render_stl_preview(
    stl_path: Path,
    output_path: Path,
    *,
    size: tuple[int, int] = (520, 320),
) -> Path:
    """Render a small orthographic PNG preview from an STL file.

    The helper is intentionally lightweight and deterministic enough for local
    evidence bundles. It is not a visual-quality judge.
    """
    if trimesh is None:
        raise RuntimeError("trimesh is required to render STL previews") from (
            _TRIMESH_IMPORT_ERROR
        )
    mesh = trimesh.load_mesh(stl_path, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
        image = Image.new("RGB", size, (232, 238, 245))
        image.save(output_path)
        return output_path

    vertices = np.asarray(mesh.vertices, dtype=float)
    vertices = vertices - vertices.mean(axis=0)
    rotation = _rotation_matrix(math.radians(-35), 0.0, math.radians(35))
    projected = vertices @ rotation.T
    xy = projected[:, :2]
    z = projected[:, 2]
    span = np.ptp(xy, axis=0)
    scale = min(
        (size[0] - 56) / max(float(span[0]), 1e-6),
        (size[1] - 56) / max(float(span[1]), 1e-6),
    )
    points = xy * scale
    points[:, 0] += size[0] / 2
    points[:, 1] = size[1] / 2 - points[:, 1]

    image = Image.new("RGB", size, (242, 246, 250))
    draw = ImageDraw.Draw(image)
    faces = np.asarray(mesh.faces)
    face_depth = z[faces].mean(axis=1)
    face_normals = np.asarray(mesh.face_normals)
    light = np.array([0.25, -0.35, 0.9])
    light = light / np.linalg.norm(light)
    order = np.argsort(face_depth)
    base = np.array([84, 145, 190])
    for face_index in order:
        face = faces[face_index]
        polygon = [(float(points[i, 0]), float(points[i, 1])) for i in face]
        shade = max(
            0.35,
            min(1.0, float(np.dot(face_normals[face_index], light)) * 0.45 + 0.65),
        )
        color = tuple(int(c * shade) for c in base)
        draw.polygon(polygon, fill=color)
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=(220, 228, 236))
    image.save(output_path)
    return output_path


def _rotation_matrix(rx: float, ry: float, rz: float) -> NDArray[np.float64]:
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return np.asarray(mz @ my @ mx, dtype=np.float64)


__all__ = ["render_stl_preview"]
