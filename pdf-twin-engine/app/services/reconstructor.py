import math
from pathlib import Path
from typing import Dict, Any, List
import numpy as np
import trimesh
import logging

logger = logging.getLogger(__name__)

# Coordinate scale: Claude returns normalised units (0-1000), convert to metres
COORD_SCALE    = 0.01   # 1000 units = 10 metres
WALL_THICKNESS = 0.2    # metres
FLOOR_SLAB_H   = 0.3    # metres (concrete slab between floors)


def reconstruct_3d(job_id: str, floor_schema: Dict[str, Any]) -> str:
    """
    Reconstructs a 3D mesh from the 2D floor plan schema and exports it as a GLB file.

    Args:
        job_id:       Unique job identifier.
        floor_schema: Structured JSON schema from interpreter.py.

    Returns:
        Absolute file path to the generated GLB model.

    Raises:
        RuntimeError: If 3D reconstruction fails.
    """
    try:
        output_dir  = Path("/tmp") / "pdf_twin_outputs" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "model.glb"

        meshes = []
        floors = floor_schema.get("floors", [])

        for floor in sorted(floors, key=lambda f: f.get("index", 0)):
            floor_index  = floor.get("index", 0)
            floor_height = float(floor.get("height_m", 3.0))
            # Stack floors: each floor sits on top of previous + slab
            base_elevation = floor_index * (floor_height + FLOOR_SLAB_H)

            # --- Wall meshes ---
            for wall in floor.get("walls", []):
                start = wall.get("start")
                end   = wall.get("end")

                if not start or not end or len(start) < 2 or len(end) < 2:
                    continue

                # Scale coordinates from normalised units → metres
                sx, sy = start[0] * COORD_SCALE, start[1] * COORD_SCALE
                ex, ey = end[0]   * COORD_SCALE, end[1]   * COORD_SCALE

                dx     = ex - sx
                dy     = ey - sy
                length = math.hypot(dx, dy)

                if length < 1e-4:
                    continue

                angle = math.atan2(dy, dx)

                # Create wall box: length × thickness × height
                wall_mesh = trimesh.creation.box(
                    extents=[length, WALL_THICKNESS, floor_height]
                )
                # Centre on origin, then rotate, then translate to world position
                wall_mesh.apply_translation([length / 2.0, 0, floor_height / 2.0])
                wall_mesh.apply_transform(
                    trimesh.transformations.rotation_matrix(angle, [0, 0, 1])
                )
                wall_mesh.apply_translation([sx, sy, base_elevation])
                wall_mesh.visual.face_colors = [200, 200, 200, 255]  # Grey walls
                meshes.append(wall_mesh)

            # --- Floor slab ---
            slab = _create_floor_slab(floor.get("walls", []), base_elevation)
            if slab is not None:
                meshes.append(slab)

        if not meshes:
            logger.warning("No walls generated from schema. Creating empty fallback mesh.")
            empty_mesh = trimesh.Trimesh()
            empty_mesh.export(str(output_path), file_type="glb")
            return str(output_path)

        # Combine all meshes into a single unified model
        combined_mesh = trimesh.util.concatenate(meshes)
        combined_mesh.export(str(output_path), file_type="glb")

        logger.info("3D model exported to %s", output_path)
        return str(output_path)

    except Exception as exc:
        logger.error("Failed to reconstruct 3D model: %s", str(exc))
        raise RuntimeError(f"3D reconstruction failed: {exc}") from exc


def _create_floor_slab(walls: List[Dict], z_base: float) -> trimesh.Trimesh | None:
    """Creates a flat concrete slab from the bounding box of all wall endpoints."""
    if not walls:
        return None
    try:
        points = []
        for w in walls:
            if w.get("start") and w.get("end"):
                points.append([w["start"][0] * COORD_SCALE, w["start"][1] * COORD_SCALE])
                points.append([w["end"][0]   * COORD_SCALE, w["end"][1]   * COORD_SCALE])

        if not points:
            return None

        pts   = np.array(points)
        min_x, min_y = pts.min(axis=0)
        max_x, max_y = pts.max(axis=0)

        slab = trimesh.creation.box(
            extents=[max_x - min_x, max_y - min_y, FLOOR_SLAB_H]
        )
        slab.apply_translation([
            (min_x + max_x) / 2,
            (min_y + max_y) / 2,
            z_base - FLOOR_SLAB_H / 2,
        ])
        slab.visual.face_colors = [150, 120, 90, 255]  # Concrete colour
        return slab

    except Exception:
        return None