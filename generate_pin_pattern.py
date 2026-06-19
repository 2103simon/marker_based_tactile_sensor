#!/usr/bin/env python3
"""Generate Inventor import points for inward-facing pins on an inner dome."""

from __future__ import annotations

import argparse
import ast
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


UNIT_NAMES = {"mm", "deg", "ul"}
GOLDEN_ANGLE_RAD = math.pi * (3.0 - math.sqrt(5.0))


@dataclass(frozen=True)
class Point:
    index: int
    ring: int
    theta_deg: float
    phi_deg: float
    x: float
    y: float
    z: float
    nx: float
    ny: float
    nz: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create XLSX point coordinates on the inner wall of a hemispherical "
            "dome. The simple workbook is intended for Inventor 3D sketch point "
            "import; the annotated workbook also includes inward normal vectors."
        )
    )
    parser.add_argument(
        "--params",
        type=Path,
        default=Path("dome-params.xml"),
        help="Inventor XML parameter file containing dome_diameter.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("pin_points_inventor.xlsx"),
        help="XLSX workbook with x,y,z columns only. Defaults to no header for Inventor import.",
    )
    parser.add_argument(
        "--normal-output",
        type=Path,
        default=Path("pin_points_with_normals.xlsx"),
        help="XLSX workbook with metadata and inward normal vectors for inspection or iLogic/API use.",
    )
    parser.add_argument(
        "--target-spacing-mm",
        type=float,
        default=4.0,
        help="Approximate center-to-center spacing between neighboring pins.",
    )
    parser.add_argument(
        "--pattern",
        choices=("rings", "fibonacci"),
        default="rings",
        help=(
            "Point distribution algorithm. rings keeps latitude-like rows; "
            "fibonacci uses the golden-angle sunflower layout on the spherical cap."
        ),
    )
    parser.add_argument(
        "--point-count",
        type=int,
        default=None,
        help=(
            "Total number of points for algorithms that support it. Currently "
            "used by fibonacci; omit to estimate from target spacing."
        ),
    )
    parser.add_argument(
        "--collision-clearance-mm",
        type=float,
        default=0.0,
        help=(
            "Extra clearance between pin cylinders. Collision checks use "
            "pin_diameter + this value as the minimum axis distance."
        ),
    )
    parser.add_argument(
        "--theta-max-deg",
        type=float,
        default=85.0,
        help="Maximum polar angle from the bottom pole. 90 reaches the cut rim.",
    )
    parser.add_argument(
        "--theta-min-deg",
        type=float,
        default=0.0,
        help=(
            "Minimum polar angle from the bottom pole. 0 includes the center "
            "unless --exclude-center is set."
        ),
    )
    parser.add_argument(
        "--exclude-center",
        action="store_true",
        help="Exclude the bottom pole point when the center pin already exists.",
    )
    parser.add_argument(
        "--include-center",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--z-origin",
        choices=("sphere_center", "bottom"),
        default="sphere_center",
        help=(
            "Unrotated coordinate origin. sphere_center gives bottom z=-R and rim z=0; "
            "bottom gives bottom z=0 and rim z=R."
        ),
    )
    parser.add_argument(
        "--rotate-x-deg",
        type=float,
        default=90.0,
        help=(
            "Rotate output points and normals around the X axis. Default 90 maps "
            "the dome ring planes from XY into XZ."
        ),
    )
    parser.add_argument(
        "--include-header",
        action="store_true",
        help="Add a header row to the x,y,z output workbook.",
    )
    parser.add_argument(
        "--precision",
        type=int,
        default=8,
        help="Decimal places written to workbook numeric cells.",
    )
    return parser.parse_args()


def read_inventor_params(path: Path) -> dict[str, float]:
    tree = ET.parse(path)
    raw_values = {
        node.findtext("name"): node.findtext("value", "")
        for node in tree.findall("./parameters/ParamWithValue")
        if node.findtext("name")
    }

    resolved: dict[str, float] = {}
    pending = dict(raw_values)

    while pending:
        progress = False
        for name, expression in list(pending.items()):
            try:
                resolved[name] = evaluate_expression(expression, resolved)
            except (KeyError, ValueError):
                continue
            del pending[name]
            progress = True

        if not progress:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(f"Could not resolve parameter value(s): {unresolved}")

    return resolved


def evaluate_expression(expression: str, variables: dict[str, float]) -> float:
    cleaned = strip_units(expression)
    if not cleaned:
        raise ValueError("empty expression")
    node = ast.parse(cleaned, mode="eval")
    return float(evaluate_ast(node.body, variables))


def strip_units(expression: str) -> str:
    return re.sub(r"\b(mm|deg|ul)\b", "", expression).strip()


def evaluate_ast(node: ast.AST, variables: dict[str, float]) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)

    if isinstance(node, ast.Name):
        if node.id in UNIT_NAMES:
            return 1.0
        if node.id not in variables:
            raise KeyError(node.id)
        return variables[node.id]

    if isinstance(node, ast.UnaryOp):
        operand = evaluate_ast(node.operand, variables)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return -operand

    if isinstance(node, ast.BinOp):
        left = evaluate_ast(node.left, variables)
        right = evaluate_ast(node.right, variables)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right

    raise ValueError(f"Unsupported expression: {ast.dump(node)}")


def generate_points(
    radius: float,
    target_spacing: float,
    theta_min_deg: float,
    theta_max_deg: float,
    z_origin: str,
    rotate_x_deg: float,
    include_center: bool,
    pattern: str,
    point_count: int | None,
) -> list[Point]:
    if radius <= 0:
        raise ValueError("dome radius must be positive")
    if target_spacing <= 0:
        raise ValueError("target spacing must be positive")
    if point_count is not None and point_count <= 0:
        raise ValueError("point count must be positive")
    if not 0 <= theta_min_deg <= theta_max_deg <= 90:
        raise ValueError("theta angles must satisfy 0 <= theta_min <= theta_max <= 90")

    theta_min = math.radians(theta_min_deg)
    theta_max = math.radians(theta_max_deg)

    if pattern == "rings":
        return generate_ring_points(
            radius=radius,
            target_spacing=target_spacing,
            theta_min=theta_min,
            theta_max=theta_max,
            z_origin=z_origin,
            rotate_x_deg=rotate_x_deg,
            include_center=include_center,
        )

    if pattern == "fibonacci":
        return generate_fibonacci_points(
            radius=radius,
            target_spacing=target_spacing,
            theta_min=theta_min,
            theta_max=theta_max,
            z_origin=z_origin,
            rotate_x_deg=rotate_x_deg,
            include_center=include_center,
            point_count=point_count,
        )

    raise ValueError(f"Unsupported pattern: {pattern}")


def generate_collision_safe_points(
    radius: float,
    target_spacing: float,
    theta_min_deg: float,
    theta_max_deg: float,
    z_origin: str,
    rotate_x_deg: float,
    include_center: bool,
    pattern: str,
    point_count: int | None,
    pin_diameter: float,
    pin_height: float,
    collision_clearance: float,
) -> tuple[list[Point], float, int | None]:
    if pin_diameter <= 0:
        raise ValueError("pin_diameter must be positive")
    if pin_height < 0:
        raise ValueError("pin_height must be non-negative")
    if collision_clearance < 0:
        raise ValueError("collision clearance must be non-negative")

    minimum_axis_distance = pin_diameter + collision_clearance
    minimum_spacing = minimum_safe_surface_spacing(
        radius=radius,
        pin_height=pin_height,
        minimum_axis_distance=minimum_axis_distance,
    )

    effective_spacing = target_spacing
    if pattern != "fibonacci" or point_count is None:
        effective_spacing = max(target_spacing, minimum_spacing)
        if effective_spacing > target_spacing:
            print(
                "Requested spacing "
                f"{format_number(target_spacing, 8)} mm is below the safe spacing "
                f"{format_number(minimum_spacing, 8)} mm for "
                f"pin_diameter={format_number(pin_diameter, 8)} mm and "
                f"pin_height={format_number(pin_height, 8)} mm; using "
                f"{format_number(effective_spacing, 8)} mm."
            )

    effective_point_count = point_count
    last_collision: tuple[int, int, float] | None = None

    for attempt in range(80):
        points = generate_points(
            radius=radius,
            target_spacing=effective_spacing,
            theta_min_deg=theta_min_deg,
            theta_max_deg=theta_max_deg,
            z_origin=z_origin,
            rotate_x_deg=rotate_x_deg,
            include_center=include_center,
            pattern=pattern,
            point_count=effective_point_count,
        )
        collision = find_first_pin_collision(
            points=points,
            pin_height=pin_height,
            minimum_axis_distance=minimum_axis_distance,
        )
        if collision is None:
            if attempt > 0:
                print(
                    "Collision fallback resolved pattern with "
                    f"spacing={format_number(effective_spacing, 8)} mm"
                    + (
                        f" and point_count={effective_point_count}."
                        if effective_point_count is not None
                        else "."
                    )
                )
            return points, effective_spacing, effective_point_count

        last_collision = collision
        first_index, second_index, distance = collision
        if pattern == "fibonacci" and effective_point_count is not None:
            next_point_count = max(1, math.floor(effective_point_count * 0.95))
            if next_point_count == effective_point_count:
                next_point_count -= 1
            if next_point_count <= 0:
                break
            print(
                "Pins would collide "
                f"(points {first_index} and {second_index}, axis distance "
                f"{format_number(distance, 8)} mm); reducing point_count from "
                f"{effective_point_count} to {next_point_count}."
            )
            effective_point_count = next_point_count
        else:
            next_spacing = effective_spacing * 1.05
            print(
                "Pins would collide "
                f"(points {first_index} and {second_index}, axis distance "
                f"{format_number(distance, 8)} mm); increasing spacing from "
                f"{format_number(effective_spacing, 8)} mm to "
                f"{format_number(next_spacing, 8)} mm."
            )
            effective_spacing = next_spacing

    if last_collision is None:
        raise ValueError("Could not generate collision-safe pin points")

    first_index, second_index, distance = last_collision
    raise ValueError(
        "Could not generate collision-safe pin points after fallback attempts. "
        f"Last collision: points {first_index} and {second_index}, axis distance "
        f"{format_number(distance, 8)} mm."
    )


def minimum_safe_surface_spacing(
    radius: float,
    pin_height: float,
    minimum_axis_distance: float,
) -> float:
    if minimum_axis_distance <= 0:
        return 0.0

    tip_radius = radius - pin_height
    if tip_radius <= 0:
        raise ValueError(
            "pin_height must be smaller than the inner dome radius for more than "
            "one inward radial pin to avoid collision near the center"
        )

    if minimum_axis_distance > 2 * tip_radius:
        raise ValueError(
            "pin diameter/clearance is too large for the pin tip radius; no "
            "collision-safe spacing exists for multiple inward radial pins"
        )

    minimum_angle = 2 * math.asin(minimum_axis_distance / (2 * tip_radius))
    return radius * minimum_angle


def generate_ring_points(
    radius: float,
    target_spacing: float,
    theta_min: float,
    theta_max: float,
    z_origin: str,
    rotate_x_deg: float,
    include_center: bool,
) -> list[Point]:
    theta_span = theta_max - theta_min

    if theta_span == 0:
        ring_count = 0
    else:
        ring_count = max(1, math.ceil((radius * theta_span) / target_spacing))

    points: list[Point] = []
    index = 1

    for ring in range(ring_count + 1):
        if ring_count == 0:
            theta = theta_min
        else:
            theta = theta_min + theta_span * ring / ring_count

        ring_radius = radius * math.sin(theta)
        if ring_radius < 1e-9 and not include_center:
            continue

        circumference = 2 * math.pi * ring_radius
        count = 1 if ring_radius < 1e-9 else max(3, round(circumference / target_spacing))
        phi_offset = 0.0 if ring % 2 == 0 else math.pi / count

        for item in range(count):
            phi = phi_offset + (2 * math.pi * item / count if count > 1 else 0.0)
            points.append(
                make_point(
                    index=index,
                    ring=ring,
                    theta=theta,
                    phi=phi,
                    radius=radius,
                    z_origin=z_origin,
                    rotate_x_deg=rotate_x_deg,
                )
            )
            index += 1

    return points


def generate_fibonacci_points(
    radius: float,
    target_spacing: float,
    theta_min: float,
    theta_max: float,
    z_origin: str,
    rotate_x_deg: float,
    include_center: bool,
    point_count: int | None,
) -> list[Point]:
    total_count = point_count or estimate_cap_point_count(radius, target_spacing, theta_min, theta_max)
    points: list[Point] = []
    index = 1
    center_is_in_range = math.isclose(theta_min, 0.0, abs_tol=1e-12)

    if include_center and center_is_in_range:
        points.append(
            make_point(
                index=index,
                ring=0,
                theta=0.0,
                phi=0.0,
                radius=radius,
                z_origin=z_origin,
                rotate_x_deg=rotate_x_deg,
            )
        )
        index += 1

    remaining_count = total_count - len(points)
    if remaining_count <= 0:
        return points

    cos_min = math.cos(theta_min)
    cos_max = math.cos(theta_max)
    cos_span = cos_min - cos_max

    for item in range(remaining_count):
        if include_center and center_is_in_range:
            area_fraction = (item + 1) / (remaining_count + 1)
        else:
            area_fraction = (item + 0.5) / remaining_count

        cos_theta = cos_min - cos_span * area_fraction
        theta = math.acos(clamp(cos_theta, cos_max, cos_min))
        phi = item * GOLDEN_ANGLE_RAD
        points.append(
            make_point(
                index=index,
                ring=item + 1,
                theta=theta,
                phi=phi,
                radius=radius,
                z_origin=z_origin,
                rotate_x_deg=rotate_x_deg,
            )
        )
        index += 1

    return points


def estimate_cap_point_count(
    radius: float,
    target_spacing: float,
    theta_min: float,
    theta_max: float,
) -> int:
    cap_area = 2 * math.pi * radius**2 * (math.cos(theta_min) - math.cos(theta_max))
    triangular_cell_area = math.sqrt(3) / 2 * target_spacing**2
    return max(1, round(cap_area / triangular_cell_area))


def make_point(
    index: int,
    ring: int,
    theta: float,
    phi: float,
    radius: float,
    z_origin: str,
    rotate_x_deg: float,
) -> Point:
    ring_radius = radius * math.sin(theta)
    x = ring_radius * math.cos(phi)
    y = ring_radius * math.sin(phi)
    z_center_origin = -radius * math.cos(theta)
    z = z_center_origin if z_origin == "sphere_center" else z_center_origin + radius

    # Inward means from the inner surface point toward the sphere center.
    nx = -x / radius
    ny = -y / radius
    nz = -z_center_origin / radius
    x, y, z = rotate_around_x(x, y, z, rotate_x_deg)
    nx, ny, nz = rotate_around_x(nx, ny, nz, rotate_x_deg)

    return Point(
        index=index,
        ring=ring,
        theta_deg=math.degrees(theta),
        phi_deg=math.degrees(phi) % 360,
        x=x,
        y=y,
        z=z,
        nx=nx,
        ny=ny,
        nz=nz,
    )


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def find_first_pin_collision(
    points: list[Point],
    pin_height: float,
    minimum_axis_distance: float,
) -> tuple[int, int, float] | None:
    axes = [(point, pin_axis_start(point), pin_axis_end(point, pin_height)) for point in points]

    for left_index, (left_point, left_start, left_end) in enumerate(axes):
        for right_point, right_start, right_end in axes[left_index + 1 :]:
            distance = segment_distance(left_start, left_end, right_start, right_end)
            if distance < minimum_axis_distance - 1e-9:
                return left_point.index, right_point.index, distance

    return None


def pin_axis_start(point: Point) -> tuple[float, float, float]:
    return point.x, point.y, point.z


def pin_axis_end(point: Point, pin_height: float) -> tuple[float, float, float]:
    return (
        point.x + point.nx * pin_height,
        point.y + point.ny * pin_height,
        point.z + point.nz * pin_height,
    )


def segment_distance(
    first_start: tuple[float, float, float],
    first_end: tuple[float, float, float],
    second_start: tuple[float, float, float],
    second_end: tuple[float, float, float],
) -> float:
    first_direction = vector_sub(first_end, first_start)
    second_direction = vector_sub(second_end, second_start)
    offset = vector_sub(first_start, second_start)

    first_length_squared = vector_dot(first_direction, first_direction)
    second_length_squared = vector_dot(second_direction, second_direction)
    second_projection = vector_dot(second_direction, offset)
    epsilon = 1e-12

    if first_length_squared <= epsilon and second_length_squared <= epsilon:
        return vector_length(vector_sub(first_start, second_start))

    if first_length_squared <= epsilon:
        first_scale = 0.0
        second_scale = clamp(second_projection / second_length_squared, 0.0, 1.0)
    else:
        first_projection = vector_dot(first_direction, offset)
        if second_length_squared <= epsilon:
            second_scale = 0.0
            first_scale = clamp(-first_projection / first_length_squared, 0.0, 1.0)
        else:
            direction_dot = vector_dot(first_direction, second_direction)
            denominator = (
                first_length_squared * second_length_squared - direction_dot * direction_dot
            )
            if denominator != 0:
                first_scale = clamp(
                    (direction_dot * second_projection - first_projection * second_length_squared)
                    / denominator,
                    0.0,
                    1.0,
                )
            else:
                first_scale = 0.0

            second_scale = (
                direction_dot * first_scale + second_projection
            ) / second_length_squared

            if second_scale < 0.0:
                second_scale = 0.0
                first_scale = clamp(-first_projection / first_length_squared, 0.0, 1.0)
            elif second_scale > 1.0:
                second_scale = 1.0
                first_scale = clamp(
                    (direction_dot - first_projection) / first_length_squared,
                    0.0,
                    1.0,
                )

    closest_first = vector_add(first_start, vector_scale(first_direction, first_scale))
    closest_second = vector_add(second_start, vector_scale(second_direction, second_scale))
    return vector_length(vector_sub(closest_first, closest_second))


def vector_add(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return left[0] + right[0], left[1] + right[1], left[2] + right[2]


def vector_sub(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return left[0] - right[0], left[1] - right[1], left[2] - right[2]


def vector_scale(vector: tuple[float, float, float], scale: float) -> tuple[float, float, float]:
    return vector[0] * scale, vector[1] * scale, vector[2] * scale


def vector_dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def vector_length(vector: tuple[float, float, float]) -> float:
    return math.sqrt(vector_dot(vector, vector))


def rotate_around_x(x: float, y: float, z: float, angle_deg: float) -> tuple[float, float, float]:
    angle = math.radians(angle_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return x, y * cosine - z * sine, y * sine + z * cosine


def write_inventor_xlsx(path: Path, points: list[Point], precision: int, header: bool) -> None:
    rows: list[list[str | float | int]] = []
    if header:
        rows.append(["x_mm", "y_mm", "z_mm"])

    for point in points:
        rows.append(
            [
                round_for_output(point.x, precision),
                round_for_output(point.y, precision),
                round_for_output(point.z, precision),
            ]
        )

    write_xlsx(path, rows, sheet_name="Pin points")


def write_normal_xlsx(path: Path, points: list[Point], precision: int) -> None:
    rows: list[list[str | float | int]] = [
        [
            "index",
            "ring",
            "theta_deg",
            "phi_deg",
            "x_mm",
            "y_mm",
            "z_mm",
            "inward_x",
            "inward_y",
            "inward_z",
        ]
    ]

    for point in points:
        rows.append(
            [
                point.index,
                point.ring,
                round_for_output(point.theta_deg, precision),
                round_for_output(point.phi_deg, precision),
                round_for_output(point.x, precision),
                round_for_output(point.y, precision),
                round_for_output(point.z, precision),
                round_for_output(point.nx, precision),
                round_for_output(point.ny, precision),
                round_for_output(point.nz, precision),
            ]
        )

    write_xlsx(path, rows, sheet_name="Pin normals")


def write_xlsx(path: Path, rows: list[list[str | float | int]], sheet_name: str) -> None:
    if path.suffix.lower() != ".xlsx":
        raise ValueError(f"XLSX output path must end with .xlsx: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_xml = build_sheet_xml(rows)
    workbook_xml = build_workbook_xml(sheet_name)

    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as xlsx:
        xlsx.writestr("[Content_Types].xml", content_types_xml())
        xlsx.writestr("_rels/.rels", package_relationships_xml())
        xlsx.writestr("xl/workbook.xml", workbook_xml)
        xlsx.writestr("xl/_rels/workbook.xml.rels", workbook_relationships_xml())
        xlsx.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def build_sheet_xml(rows: list[list[str | float | int]]) -> str:
    xml_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = [
            build_cell_xml(column_index, row_index, value)
            for column_index, value in enumerate(row, start=1)
        ]
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        f'{"".join(xml_rows)}'
        "</sheetData>"
        "</worksheet>"
    )


def build_cell_xml(column_index: int, row_index: int, value: str | float | int) -> str:
    cell_ref = f"{column_name(column_index)}{row_index}"
    if isinstance(value, str):
        return f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    return f'<c r="{cell_ref}"><v>{format_number(float(value), 12)}</v></c>'


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def build_workbook_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        f'<sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )


def content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )


def package_relationships_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def workbook_relationships_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )


def round_for_output(value: float, precision: int) -> float:
    if abs(value) < 0.5 * 10**-precision:
        return 0.0
    return round(value, precision)


def format_number(value: float, precision: int) -> str:
    if abs(value) < 0.5 * 10**-precision:
        value = 0.0
    text = f"{value:.{precision}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def main() -> None:
    args = parse_args()
    params = read_inventor_params(args.params)

    try:
        dome_diameter = params["dome_diameter"]
        pin_diameter = params["pin_diameter"]
        pin_height = params["pin_height"]
    except KeyError as exc:
        raise SystemExit(f"{exc.args[0]} was not found in the parameter file") from exc

    radius = dome_diameter / 2
    points, effective_spacing, effective_point_count = generate_collision_safe_points(
        radius=radius,
        target_spacing=args.target_spacing_mm,
        theta_min_deg=args.theta_min_deg,
        theta_max_deg=args.theta_max_deg,
        z_origin=args.z_origin,
        rotate_x_deg=args.rotate_x_deg,
        include_center=not args.exclude_center,
        pattern=args.pattern,
        point_count=args.point_count,
        pin_diameter=pin_diameter,
        pin_height=pin_height,
        collision_clearance=args.collision_clearance_mm,
    )

    write_inventor_xlsx(args.output, points, args.precision, args.include_header)
    write_normal_xlsx(args.normal_output, points, args.precision)

    print(f"Read dome_diameter={format_number(dome_diameter, args.precision)} mm")
    print(f"Read pin_diameter={format_number(pin_diameter, args.precision)} mm")
    print(f"Read pin_height={format_number(pin_height, args.precision)} mm")
    print(f"Generated {len(points)} point(s) on radius {format_number(radius, args.precision)} mm")
    print(f"Pattern: {args.pattern}")
    if effective_spacing != args.target_spacing_mm:
        print(f"Effective spacing: {format_number(effective_spacing, args.precision)} mm")
    if effective_point_count != args.point_count:
        print(f"Effective point_count: {effective_point_count}")
    print(f"Applied X-axis rotation: {format_number(args.rotate_x_deg, args.precision)} deg")
    print(f"Inventor x,y,z XLSX: {args.output}")
    print(f"Annotated normals XLSX: {args.normal_output}")


if __name__ == "__main__":
    main()
