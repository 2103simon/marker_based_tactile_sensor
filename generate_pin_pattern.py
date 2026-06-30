#!/usr/bin/env python3
"""Generate Inventor import points for inward-facing pins on an inner dome."""

from __future__ import annotations

import argparse
import ast
import bisect
import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


UNIT_NAMES = {"mm", "deg", "ul"}
GOLDEN_ANGLE_RAD = math.pi * (3.0 - math.sqrt(5.0))
FIBONACCI_CENTER_RING_COUNT = 6


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


@dataclass(frozen=True)
class SpacingGradient:
    center: float
    periphery: float

    def spacing_at_fraction(self, fraction: float) -> float:
        clamped_fraction = clamp(fraction, 0.0, 1.0)
        return self.center + (self.periphery - self.center) * clamped_fraction

    def is_uniform(self) -> bool:
        return math.isclose(self.center, self.periphery, rel_tol=1e-12, abs_tol=1e-12)

    def clamped_minimum(self, minimum_spacing: float) -> "SpacingGradient":
        return SpacingGradient(
            center=max(self.center, minimum_spacing),
            periphery=max(self.periphery, minimum_spacing),
        )

    def scaled(self, factor: float) -> "SpacingGradient":
        return SpacingGradient(
            center=self.center * factor,
            periphery=self.periphery * factor,
        )


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
        "--preview-output",
        type=Path,
        default=Path("pin_pattern_preview.html"),
        help="Interactive HTML preview output path.",
    )
    preview_group = parser.add_mutually_exclusive_group()
    preview_group.add_argument(
        "--create-preview",
        dest="create_preview",
        action="store_true",
        default=True,
        help="Create the interactive HTML preview. Enabled by default.",
    )
    preview_group.add_argument(
        "--no-preview",
        dest="create_preview",
        action="store_false",
        help="Skip the interactive HTML preview.",
    )
    parser.add_argument(
        "--preview-show-helpers",
        action="store_true",
        help=(
            "Add helper traces to the preview: inward axis lines, direction cones, "
            "base point markers, and sphere center. By default the preview draws "
            "pin cylinders only."
        ),
    )
    parser.add_argument(
        "--target-spacing-mm",
        type=float,
        default=4.0,
        help="Approximate center-to-center spacing between neighboring pins.",
    )
    parser.add_argument(
        "--center-spacing-mm",
        type=float,
        default=None,
        help=(
            "Optional spacing at the dome center. Defaults to --target-spacing-mm. "
            "Use with --periphery-spacing-mm to create a density gradient."
        ),
    )
    parser.add_argument(
        "--periphery-spacing-mm",
        "--rim-spacing-mm",
        "--edge-spacing-mm",
        dest="periphery_spacing_mm",
        type=float,
        default=None,
        help=(
            "Optional spacing at theta-max near the rim/periphery. Defaults to "
            "--target-spacing-mm."
        ),
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


def make_spacing_gradient(
    target_spacing: float,
    center_spacing: float | None,
    periphery_spacing: float | None,
) -> SpacingGradient:
    if target_spacing <= 0:
        raise ValueError("target spacing must be positive")

    center = target_spacing if center_spacing is None else center_spacing
    periphery = target_spacing if periphery_spacing is None else periphery_spacing

    if center <= 0:
        raise ValueError("center spacing must be positive")
    if periphery <= 0:
        raise ValueError("periphery spacing must be positive")

    return SpacingGradient(center=center, periphery=periphery)


def spacing_gradients_equal(left: SpacingGradient, right: SpacingGradient) -> bool:
    return math.isclose(left.center, right.center) and math.isclose(
        left.periphery, right.periphery
    )


def format_spacing_gradient(gradient: SpacingGradient, precision: int) -> str:
    if gradient.is_uniform():
        return f"spacing={format_number(gradient.center, precision)} mm"
    return (
        f"center_spacing={format_number(gradient.center, precision)} mm, "
        f"periphery_spacing={format_number(gradient.periphery, precision)} mm"
    )


def generate_points(
    radius: float,
    target_spacing: float,
    center_spacing: float | None,
    periphery_spacing: float | None,
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
    if point_count is not None and point_count <= 0:
        raise ValueError("point count must be positive")
    if not 0 <= theta_min_deg <= theta_max_deg <= 90:
        raise ValueError("theta angles must satisfy 0 <= theta_min <= theta_max <= 90")

    spacing_gradient = make_spacing_gradient(
        target_spacing=target_spacing,
        center_spacing=center_spacing,
        periphery_spacing=periphery_spacing,
    )
    theta_min = math.radians(theta_min_deg)
    theta_max = math.radians(theta_max_deg)

    if pattern == "rings":
        return generate_ring_points(
            radius=radius,
            spacing_gradient=spacing_gradient,
            theta_min=theta_min,
            theta_max=theta_max,
            z_origin=z_origin,
            rotate_x_deg=rotate_x_deg,
            include_center=include_center,
        )

    if pattern == "fibonacci":
        return generate_fibonacci_points(
            radius=radius,
            spacing_gradient=spacing_gradient,
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
    center_spacing: float | None,
    periphery_spacing: float | None,
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
) -> tuple[list[Point], SpacingGradient, int | None]:
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

    requested_gradient = make_spacing_gradient(
        target_spacing=target_spacing,
        center_spacing=center_spacing,
        periphery_spacing=periphery_spacing,
    )
    effective_gradient = requested_gradient
    if pattern != "fibonacci" or point_count is None:
        effective_gradient = requested_gradient.clamped_minimum(minimum_spacing)
        if not spacing_gradients_equal(effective_gradient, requested_gradient):
            if requested_gradient.is_uniform():
                print(
                    "Requested spacing "
                    f"{format_number(requested_gradient.center, 8)} mm is below the "
                    f"safe spacing {format_number(minimum_spacing, 8)} mm for "
                    f"pin_diameter={format_number(pin_diameter, 8)} mm and "
                    f"pin_height={format_number(pin_height, 8)} mm; using "
                    f"{format_number(effective_gradient.center, 8)} mm."
                )
            else:
                print(
                    "Requested spacing gradient includes spacing below the safe "
                    f"spacing {format_number(minimum_spacing, 8)} mm for "
                    f"pin_diameter={format_number(pin_diameter, 8)} mm and "
                    f"pin_height={format_number(pin_height, 8)} mm; using "
                    f"{format_spacing_gradient(effective_gradient, 8)}."
                )

    effective_point_count = point_count
    last_collision: tuple[int, int, float] | None = None

    for attempt in range(80):
        points = generate_points(
            radius=radius,
            target_spacing=target_spacing,
            center_spacing=effective_gradient.center,
            periphery_spacing=effective_gradient.periphery,
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
                    f"{format_spacing_gradient(effective_gradient, 8)}"
                    + (
                        f" and point_count={effective_point_count}."
                        if effective_point_count is not None
                        else "."
                    )
                )
            return points, effective_gradient, effective_point_count

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
            next_gradient = effective_gradient.scaled(1.05)
            print(
                "Pins would collide "
                f"(points {first_index} and {second_index}, axis distance "
                f"{format_number(distance, 8)} mm); increasing spacing from "
                f"{format_spacing_gradient(effective_gradient, 8)} to "
                f"{format_spacing_gradient(next_gradient, 8)}."
            )
            effective_gradient = next_gradient

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
    spacing_gradient: SpacingGradient,
    theta_min: float,
    theta_max: float,
    z_origin: str,
    rotate_x_deg: float,
    include_center: bool,
) -> list[Point]:
    theta_span = theta_max - theta_min

    if theta_span == 0:
        ring_count = 0
    elif spacing_gradient.is_uniform():
        ring_count = max(1, math.ceil((radius * theta_span) / spacing_gradient.center))
    else:
        ring_count = max(1, math.ceil(meridian_spacing_units(radius, theta_span, spacing_gradient)))

    points: list[Point] = []
    index = 1

    for ring in range(ring_count + 1):
        if ring_count == 0:
            theta = theta_min
        elif spacing_gradient.is_uniform():
            theta = theta_min + theta_span * ring / ring_count
        else:
            theta = theta_for_meridian_fraction(
                theta_min=theta_min,
                theta_max=theta_max,
                spacing_gradient=spacing_gradient,
                fraction=ring / ring_count,
            )

        ring_radius = radius * math.sin(theta)
        if ring_radius < 1e-9 and not include_center:
            continue

        spacing = spacing_gradient.spacing_at_fraction(
            normalized_theta_fraction(theta, theta_min, theta_max)
        )
        circumference = 2 * math.pi * ring_radius
        count = 1 if ring_radius < 1e-9 else max(3, round(circumference / spacing))
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
    spacing_gradient: SpacingGradient,
    theta_min: float,
    theta_max: float,
    z_origin: str,
    rotate_x_deg: float,
    include_center: bool,
    point_count: int | None,
) -> list[Point]:
    if spacing_gradient.is_uniform():
        total_count = point_count or estimate_cap_point_count(
            radius,
            spacing_gradient.center,
            theta_min,
            theta_max,
        )
    else:
        total_density_cdf = build_area_density_cdf(theta_min, theta_max, spacing_gradient)
        total_count = point_count or estimate_gradient_cap_point_count(
            radius=radius,
            theta_min=theta_min,
            theta_max=theta_max,
            density_cdf=total_density_cdf,
        )

    points: list[Point] = []
    index = 1
    center_is_in_range = math.isclose(theta_min, 0.0, abs_tol=1e-12)
    fibonacci_theta_min = theta_min
    fibonacci_phi_offset = 0.0
    fibonacci_ring_start = 1

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

        center_ring_count = min(FIBONACCI_CENTER_RING_COUNT, total_count - len(points))
        center_spacing = spacing_gradient.center
        center_ring_theta = min(theta_max, center_spacing / radius)
        if center_ring_count > 0 and center_ring_theta > 1e-12:
            for item in range(center_ring_count):
                phi = 2 * math.pi * item / center_ring_count
                points.append(
                    make_point(
                        index=index,
                        ring=1,
                        theta=center_ring_theta,
                        phi=phi,
                        radius=radius,
                        z_origin=z_origin,
                        rotate_x_deg=rotate_x_deg,
                    )
                )
                index += 1

            core_point_count = 1 + center_ring_count
            fibonacci_theta_min = min(
                theta_max,
                symmetric_core_boundary_theta(
                    radius=radius,
                    spacing=center_spacing,
                    point_count=core_point_count,
                ),
            )
            fibonacci_phi_offset = 0.5
            fibonacci_ring_start = 2

    remaining_count = total_count - len(points)
    if remaining_count <= 0:
        return points

    if spacing_gradient.is_uniform():
        cos_min = math.cos(fibonacci_theta_min)
        cos_max = math.cos(theta_max)
        cos_span = cos_min - cos_max
    else:
        density_cdf = build_area_density_cdf(fibonacci_theta_min, theta_max, spacing_gradient)
        density_cumulative_values = [value for _, value in density_cdf]

    for item in range(remaining_count):
        area_fraction = (item + 0.5) / remaining_count

        if spacing_gradient.is_uniform():
            cos_theta = cos_min - cos_span * area_fraction
            theta = math.acos(clamp(cos_theta, cos_max, cos_min))
        else:
            theta = theta_for_area_density_fraction(
                theta_min=fibonacci_theta_min,
                theta_max=theta_max,
                density_cdf=density_cdf,
                density_cumulative_values=density_cumulative_values,
                fraction=area_fraction,
            )

        phi = (item + fibonacci_phi_offset) * GOLDEN_ANGLE_RAD
        points.append(
            make_point(
                index=index,
                ring=fibonacci_ring_start + item,
                theta=theta,
                phi=phi,
                radius=radius,
                z_origin=z_origin,
                rotate_x_deg=rotate_x_deg,
            )
        )
        index += 1

    return points


def symmetric_core_boundary_theta(radius: float, spacing: float, point_count: int) -> float:
    if radius <= 0:
        raise ValueError("radius must be positive")
    if spacing <= 0:
        raise ValueError("spacing must be positive")
    if point_count <= 0:
        return 0.0

    covered_surface_radius = spacing * math.sqrt(point_count * math.sqrt(3.0) / (2 * math.pi))
    return covered_surface_radius / radius


def normalized_theta_fraction(theta: float, theta_min: float, theta_max: float) -> float:
    theta_span = theta_max - theta_min
    if math.isclose(theta_span, 0.0, abs_tol=1e-12):
        return 0.0
    return clamp((theta - theta_min) / theta_span, 0.0, 1.0)


def meridian_spacing_units(
    radius: float,
    theta_span: float,
    spacing_gradient: SpacingGradient,
) -> float:
    if math.isclose(theta_span, 0.0, abs_tol=1e-12):
        return 0.0

    spacing_delta = spacing_gradient.periphery - spacing_gradient.center
    if math.isclose(spacing_delta, 0.0, abs_tol=1e-12):
        return radius * theta_span / spacing_gradient.center

    spacing_ratio = spacing_gradient.periphery / spacing_gradient.center
    return radius * theta_span * math.log(spacing_ratio) / spacing_delta


def theta_for_meridian_fraction(
    theta_min: float,
    theta_max: float,
    spacing_gradient: SpacingGradient,
    fraction: float,
) -> float:
    theta_span = theta_max - theta_min
    clamped_fraction = clamp(fraction, 0.0, 1.0)
    if math.isclose(theta_span, 0.0, abs_tol=1e-12):
        return theta_min

    spacing_delta = spacing_gradient.periphery - spacing_gradient.center
    if math.isclose(spacing_delta, 0.0, abs_tol=1e-12):
        return theta_min + theta_span * clamped_fraction

    spacing_ratio = spacing_gradient.periphery / spacing_gradient.center
    theta_fraction = (
        spacing_gradient.center * (spacing_ratio**clamped_fraction - 1.0) / spacing_delta
    )
    return theta_min + theta_span * theta_fraction


def estimate_cap_point_count(
    radius: float,
    target_spacing: float,
    theta_min: float,
    theta_max: float,
) -> int:
    cap_area = 2 * math.pi * radius**2 * (math.cos(theta_min) - math.cos(theta_max))
    triangular_cell_area = math.sqrt(3) / 2 * target_spacing**2
    return max(1, round(cap_area / triangular_cell_area))


def estimate_gradient_cap_point_count(
    radius: float,
    theta_min: float,
    theta_max: float,
    density_cdf: list[tuple[float, float]],
) -> int:
    theta_span = theta_max - theta_min
    if math.isclose(theta_span, 0.0, abs_tol=1e-12):
        return 1

    density_integral = density_cdf[-1][1]
    count = 2 * math.pi * radius**2 * theta_span * density_integral * 2 / math.sqrt(3)
    return max(1, round(count))


def build_area_density_cdf(
    theta_min: float,
    theta_max: float,
    spacing_gradient: SpacingGradient,
) -> list[tuple[float, float]]:
    sample_count = 2048
    theta_span = theta_max - theta_min
    cdf = [(0.0, 0.0)]
    if math.isclose(theta_span, 0.0, abs_tol=1e-12):
        cdf.append((1.0, 0.0))
        return cdf

    total = 0.0
    previous_weight = area_density_weight(theta_min, theta_min, theta_max, spacing_gradient)
    for sample in range(1, sample_count + 1):
        fraction = sample / sample_count
        theta = theta_min + theta_span * fraction
        weight = area_density_weight(theta, theta_min, theta_max, spacing_gradient)
        total += (previous_weight + weight) * 0.5 / sample_count
        cdf.append((fraction, total))
        previous_weight = weight

    return cdf


def area_density_weight(
    theta: float,
    theta_min: float,
    theta_max: float,
    spacing_gradient: SpacingGradient,
) -> float:
    spacing = spacing_gradient.spacing_at_fraction(
        normalized_theta_fraction(theta, theta_min, theta_max)
    )
    return math.sin(theta) / spacing**2


def theta_for_area_density_fraction(
    theta_min: float,
    theta_max: float,
    density_cdf: list[tuple[float, float]],
    density_cumulative_values: list[float],
    fraction: float,
) -> float:
    theta_span = theta_max - theta_min
    total = density_cdf[-1][1]
    if math.isclose(theta_span, 0.0, abs_tol=1e-12) or total <= 0:
        return theta_min

    target = total * clamp(fraction, 0.0, 1.0)
    position = bisect.bisect_left(density_cumulative_values, target)
    if position <= 0:
        return theta_min
    if position >= len(density_cdf):
        return theta_max

    previous_fraction, previous_value = density_cdf[position - 1]
    next_fraction, next_value = density_cdf[position]
    value_span = next_value - previous_value
    if value_span <= 0:
        theta_fraction = previous_fraction
    else:
        segment_fraction = (target - previous_value) / value_span
        theta_fraction = previous_fraction + (next_fraction - previous_fraction) * segment_fraction

    return theta_min + theta_span * theta_fraction


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


def vector_cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def vector_length(vector: tuple[float, float, float]) -> float:
    return math.sqrt(vector_dot(vector, vector))


def vector_normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = vector_length(vector)
    if length <= 1e-12:
        raise ValueError("cannot normalize a zero-length vector")
    return vector_scale(vector, 1.0 / length)


def rotate_around_x(x: float, y: float, z: float, angle_deg: float) -> tuple[float, float, float]:
    angle = math.radians(angle_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return x, y * cosine - z * sine, y * sine + z * cosine


def write_preview_html(
    path: Path,
    points: list[Point],
    inner_radius: float,
    wall_thickness: float,
    pin_diameter: float,
    pin_height: float,
    theta_max_deg: float,
    z_origin: str,
    rotate_x_deg: float,
    show_helpers: bool,
    precision: int,
) -> None:
    if path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError(f"Preview output path must end with .html or .htm: {path}")
    if wall_thickness < 0:
        raise ValueError("wall_thickness must be non-negative")

    path.parent.mkdir(parents=True, exist_ok=True)
    theta_max = math.radians(theta_max_deg)
    outer_radius = inner_radius + wall_thickness

    traces: list[dict[str, object]] = [
        build_spherical_cap_mesh_trace(
            name="Inner dome surface",
            radius=inner_radius,
            inner_radius=inner_radius,
            theta_max=theta_max,
            z_origin=z_origin,
            rotate_x_deg=rotate_x_deg,
            color="#60a5fa",
            opacity=0.24,
        )
    ]

    if wall_thickness > 0:
        traces.append(
            build_spherical_cap_mesh_trace(
                name="Outer dome surface",
                radius=outer_radius,
                inner_radius=inner_radius,
                theta_max=theta_max,
                z_origin=z_origin,
                rotate_x_deg=rotate_x_deg,
                color="#94a3b8",
                opacity=0.14,
            )
        )
        traces.append(
            build_rim_wall_mesh_trace(
                name="Cut rim wall",
                inner_radius=inner_radius,
                outer_radius=outer_radius,
                theta=theta_max,
                z_origin=z_origin,
                rotate_x_deg=rotate_x_deg,
            )
        )

    pin_mesh = build_pin_cylinder_mesh_trace(
        points=points,
        pin_diameter=pin_diameter,
        pin_height=pin_height,
    )
    if pin_mesh is not None:
        traces.append(pin_mesh)

    if show_helpers:
        sphere_center = sphere_center_point(inner_radius, z_origin, rotate_x_deg)
        traces.extend(
            [
                build_pin_axis_trace(points, pin_height),
                build_pin_cone_trace(points, pin_diameter, pin_height),
                build_pin_point_trace(points),
                build_sphere_center_trace(sphere_center),
            ]
        )

    title = (
        "Pin Pattern Preview "
        f"({len(points)} pins, inner diameter {format_number(inner_radius * 2, precision)} mm, "
        f"wall {format_number(wall_thickness, precision)} mm)"
    )
    html = build_plotly_html(traces, title)
    path.write_text(html, encoding="utf-8")


def build_plotly_html(traces: list[dict[str, object]], title: str) -> str:
    traces_json = json.dumps(traces, separators=(",", ":"))
    layout = {
        "title": {"text": title},
        "paper_bgcolor": "#f8fafc",
        "plot_bgcolor": "#f8fafc",
        "margin": {"l": 0, "r": 0, "t": 48, "b": 0},
        "legend": {"orientation": "h", "y": 0.02},
        "scene": {
            "aspectmode": "data",
            "xaxis": {"title": "X mm", "backgroundcolor": "#f8fafc"},
            "yaxis": {"title": "Y mm", "backgroundcolor": "#f8fafc"},
            "zaxis": {"title": "Z mm", "backgroundcolor": "#f8fafc"},
            "camera": {"eye": {"x": 1.55, "y": -1.85, "z": 1.25}},
        },
    }
    layout_json = json.dumps(layout, separators=(",", ":"))
    config = {
        "responsive": True,
        "displaylogo": False,
        "toImageButtonOptions": {"format": "png", "filename": "pin_pattern_preview"},
    }
    config_json = json.dumps(config, separators=(",", ":"))
    safe_title = escape(title)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    html, body, #plot {{
      width: 100%;
      height: 100%;
      margin: 0;
      background: #f8fafc;
      font-family: Arial, sans-serif;
    }}
    .fallback {{
      position: absolute;
      left: 16px;
      bottom: 12px;
      color: #475569;
      font-size: 12px;
      z-index: 1;
    }}
  </style>
</head>
<body>
  <div id="plot"></div>
  <div class="fallback">Drag to rotate. Scroll to zoom. Shift-drag to pan.</div>
  <script>
    const traces = {traces_json};
    const layout = {layout_json};
    const config = {config_json};
    Plotly.newPlot("plot", traces, layout, config);
  </script>
</body>
</html>
"""


def build_spherical_cap_mesh_trace(
    name: str,
    radius: float,
    inner_radius: float,
    theta_max: float,
    z_origin: str,
    rotate_x_deg: float,
    color: str,
    opacity: float,
) -> dict[str, object]:
    vertices, faces = spherical_cap_mesh(
        radius=radius,
        inner_radius=inner_radius,
        theta_max=theta_max,
        z_origin=z_origin,
        rotate_x_deg=rotate_x_deg,
        theta_steps=28,
        phi_steps=96,
    )
    x, y, z = split_vertices(vertices)
    i, j, k = split_faces(faces)
    return {
        "type": "mesh3d",
        "name": name,
        "x": x,
        "y": y,
        "z": z,
        "i": i,
        "j": j,
        "k": k,
        "color": color,
        "opacity": opacity,
        "flatshading": False,
        "hoverinfo": "skip",
        "lighting": {"ambient": 0.55, "diffuse": 0.7, "roughness": 0.8},
    }


def build_rim_wall_mesh_trace(
    name: str,
    inner_radius: float,
    outer_radius: float,
    theta: float,
    z_origin: str,
    rotate_x_deg: float,
) -> dict[str, object]:
    phi_steps = 96
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    for item in range(phi_steps):
        phi = 2 * math.pi * item / phi_steps
        vertices.append(
            spherical_preview_point(
                radius=inner_radius,
                inner_radius=inner_radius,
                theta=theta,
                phi=phi,
                z_origin=z_origin,
                rotate_x_deg=rotate_x_deg,
            )
        )
        vertices.append(
            spherical_preview_point(
                radius=outer_radius,
                inner_radius=inner_radius,
                theta=theta,
                phi=phi,
                z_origin=z_origin,
                rotate_x_deg=rotate_x_deg,
            )
        )

    for item in range(phi_steps):
        next_item = (item + 1) % phi_steps
        inner = item * 2
        outer = inner + 1
        next_inner = next_item * 2
        next_outer = next_inner + 1
        faces.append((inner, next_inner, next_outer))
        faces.append((inner, next_outer, outer))

    x, y, z = split_vertices(vertices)
    i, j, k = split_faces(faces)
    return {
        "type": "mesh3d",
        "name": name,
        "x": x,
        "y": y,
        "z": z,
        "i": i,
        "j": j,
        "k": k,
        "color": "#64748b",
        "opacity": 0.32,
        "hoverinfo": "skip",
    }


def spherical_cap_mesh(
    radius: float,
    inner_radius: float,
    theta_max: float,
    z_origin: str,
    rotate_x_deg: float,
    theta_steps: int,
    phi_steps: int,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    vertices = [
        spherical_preview_point(
            radius=radius,
            inner_radius=inner_radius,
            theta=0.0,
            phi=0.0,
            z_origin=z_origin,
            rotate_x_deg=rotate_x_deg,
        )
    ]
    ring_indices: list[list[int]] = []
    faces: list[tuple[int, int, int]] = []

    for theta_index in range(1, theta_steps + 1):
        theta = theta_max * theta_index / theta_steps
        ring: list[int] = []
        for phi_index in range(phi_steps):
            phi = 2 * math.pi * phi_index / phi_steps
            ring.append(len(vertices))
            vertices.append(
                spherical_preview_point(
                    radius=radius,
                    inner_radius=inner_radius,
                    theta=theta,
                    phi=phi,
                    z_origin=z_origin,
                    rotate_x_deg=rotate_x_deg,
                )
            )
        ring_indices.append(ring)

    if not ring_indices:
        return vertices, faces

    first_ring = ring_indices[0]
    for phi_index in range(phi_steps):
        faces.append((0, first_ring[phi_index], first_ring[(phi_index + 1) % phi_steps]))

    for ring_index in range(len(ring_indices) - 1):
        current_ring = ring_indices[ring_index]
        next_ring = ring_indices[ring_index + 1]
        for phi_index in range(phi_steps):
            current = current_ring[phi_index]
            current_next = current_ring[(phi_index + 1) % phi_steps]
            next_current = next_ring[phi_index]
            next_next = next_ring[(phi_index + 1) % phi_steps]
            faces.append((current, next_current, next_next))
            faces.append((current, next_next, current_next))

    return vertices, faces


def spherical_preview_point(
    radius: float,
    inner_radius: float,
    theta: float,
    phi: float,
    z_origin: str,
    rotate_x_deg: float,
) -> tuple[float, float, float]:
    ring_radius = radius * math.sin(theta)
    x = ring_radius * math.cos(phi)
    y = ring_radius * math.sin(phi)
    origin_shift = 0.0 if z_origin == "sphere_center" else inner_radius
    z = -radius * math.cos(theta) + origin_shift
    return rotate_around_x(x, y, z, rotate_x_deg)


def build_pin_cylinder_mesh_trace(
    points: list[Point],
    pin_diameter: float,
    pin_height: float,
) -> dict[str, object] | None:
    if pin_diameter <= 0 or pin_height <= 0 or not points:
        return None

    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    sides = 14
    radius = pin_diameter / 2

    for point in points:
        start = pin_axis_start(point)
        end = pin_axis_end(point, pin_height)
        axis = vector_normalize(vector_sub(end, start))
        first_basis, second_basis = perpendicular_basis(axis)
        start_center_index = len(vertices)
        vertices.append(start)
        end_center_index = len(vertices)
        vertices.append(end)
        start_indices: list[int] = []
        end_indices: list[int] = []

        for side in range(sides):
            angle = 2 * math.pi * side / sides
            offset = vector_add(
                vector_scale(first_basis, radius * math.cos(angle)),
                vector_scale(second_basis, radius * math.sin(angle)),
            )
            start_indices.append(len(vertices))
            vertices.append(vector_add(start, offset))
            end_indices.append(len(vertices))
            vertices.append(vector_add(end, offset))

        for side in range(sides):
            next_side = (side + 1) % sides
            start_current = start_indices[side]
            start_next = start_indices[next_side]
            end_current = end_indices[side]
            end_next = end_indices[next_side]
            faces.append((start_current, start_next, end_next))
            faces.append((start_current, end_next, end_current))
            faces.append((start_center_index, start_current, start_next))
            faces.append((end_center_index, end_next, end_current))

    x, y, z = split_vertices(vertices)
    i, j, k = split_faces(faces)
    return {
        "type": "mesh3d",
        "name": "Pin cylinders",
        "x": x,
        "y": y,
        "z": z,
        "i": i,
        "j": j,
        "k": k,
        "color": "#f97316",
        "opacity": 0.74,
        "flatshading": False,
        "hoverinfo": "skip",
        "lighting": {"ambient": 0.5, "diffuse": 0.8, "roughness": 0.55},
    }


def build_pin_axis_trace(points: list[Point], pin_height: float) -> dict[str, object]:
    x: list[float | None] = []
    y: list[float | None] = []
    z: list[float | None] = []

    for point in points:
        start = pin_axis_start(point)
        end = pin_axis_end(point, pin_height)
        x.extend([start[0], end[0], None])
        y.extend([start[1], end[1], None])
        z.extend([start[2], end[2], None])

    return {
        "type": "scatter3d",
        "mode": "lines",
        "name": "Inward pin axes",
        "x": x,
        "y": y,
        "z": z,
        "line": {"color": "#7c2d12", "width": 3},
        "hoverinfo": "skip",
    }


def build_pin_cone_trace(
    points: list[Point],
    pin_diameter: float,
    pin_height: float,
) -> dict[str, object]:
    tip_points = [pin_axis_end(point, pin_height) for point in points]
    return {
        "type": "cone",
        "name": "Pin direction",
        "x": [point[0] for point in tip_points],
        "y": [point[1] for point in tip_points],
        "z": [point[2] for point in tip_points],
        "u": [point.nx * pin_height for point in points],
        "v": [point.ny * pin_height for point in points],
        "w": [point.nz * pin_height for point in points],
        "anchor": "tip",
        "sizemode": "absolute",
        "sizeref": max(pin_diameter * 2.4, pin_height * 0.35, 0.1),
        "colorscale": [[0.0, "#ea580c"], [1.0, "#ea580c"]],
        "showscale": False,
        "hoverinfo": "skip",
    }


def build_pin_point_trace(points: list[Point]) -> dict[str, object]:
    return {
        "type": "scatter3d",
        "mode": "markers",
        "name": "Pin base points",
        "x": [point.x for point in points],
        "y": [point.y for point in points],
        "z": [point.z for point in points],
        "marker": {"size": 3, "color": "#111827"},
        "text": [
            f"Point {point.index}<br>theta={format_number(point.theta_deg, 4)} deg"
            for point in points
        ],
        "hoverinfo": "text",
    }


def build_sphere_center_trace(center: tuple[float, float, float]) -> dict[str, object]:
    return {
        "type": "scatter3d",
        "mode": "markers",
        "name": "Sphere center",
        "x": [center[0]],
        "y": [center[1]],
        "z": [center[2]],
        "marker": {"size": 7, "color": "#2563eb", "symbol": "diamond"},
        "hoverinfo": "name",
    }


def sphere_center_point(
    inner_radius: float,
    z_origin: str,
    rotate_x_deg: float,
) -> tuple[float, float, float]:
    z = 0.0 if z_origin == "sphere_center" else inner_radius
    return rotate_around_x(0.0, 0.0, z, rotate_x_deg)


def perpendicular_basis(
    axis: tuple[float, float, float],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    reference = (0.0, 0.0, 1.0) if abs(axis[2]) < 0.9 else (0.0, 1.0, 0.0)
    first_basis = vector_normalize(vector_cross(axis, reference))
    second_basis = vector_normalize(vector_cross(axis, first_basis))
    return first_basis, second_basis


def split_vertices(
    vertices: list[tuple[float, float, float]],
) -> tuple[list[float], list[float], list[float]]:
    return (
        [vertex[0] for vertex in vertices],
        [vertex[1] for vertex in vertices],
        [vertex[2] for vertex in vertices],
    )


def split_faces(
    faces: list[tuple[int, int, int]],
) -> tuple[list[int], list[int], list[int]]:
    return (
        [face[0] for face in faces],
        [face[1] for face in faces],
        [face[2] for face in faces],
    )


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

    wall_thickness = params.get("wall_thickness", 0.0)
    if wall_thickness < 0:
        raise SystemExit("wall_thickness must be non-negative")

    radius = dome_diameter / 2
    requested_gradient = make_spacing_gradient(
        target_spacing=args.target_spacing_mm,
        center_spacing=args.center_spacing_mm,
        periphery_spacing=args.periphery_spacing_mm,
    )
    points, effective_gradient, effective_point_count = generate_collision_safe_points(
        radius=radius,
        target_spacing=args.target_spacing_mm,
        center_spacing=args.center_spacing_mm,
        periphery_spacing=args.periphery_spacing_mm,
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
    if args.create_preview:
        write_preview_html(
            path=args.preview_output,
            points=points,
            inner_radius=radius,
            wall_thickness=wall_thickness,
            pin_diameter=pin_diameter,
            pin_height=pin_height,
            theta_max_deg=args.theta_max_deg,
            z_origin=args.z_origin,
            rotate_x_deg=args.rotate_x_deg,
            show_helpers=args.preview_show_helpers,
            precision=args.precision,
        )

    print(f"Read dome_diameter={format_number(dome_diameter, args.precision)} mm")
    print(f"Read wall_thickness={format_number(wall_thickness, args.precision)} mm")
    print(f"Read pin_diameter={format_number(pin_diameter, args.precision)} mm")
    print(f"Read pin_height={format_number(pin_height, args.precision)} mm")
    print(f"Generated {len(points)} point(s) on radius {format_number(radius, args.precision)} mm")
    print(f"Pattern: {args.pattern}")
    if not requested_gradient.is_uniform():
        print(f"Requested spacing gradient: {format_spacing_gradient(requested_gradient, args.precision)}")
    if not spacing_gradients_equal(effective_gradient, requested_gradient):
        if requested_gradient.is_uniform() and effective_gradient.is_uniform():
            print(f"Effective spacing: {format_number(effective_gradient.center, args.precision)} mm")
        else:
            print(f"Effective spacing gradient: {format_spacing_gradient(effective_gradient, args.precision)}")
    if effective_point_count != args.point_count:
        print(f"Effective point_count: {effective_point_count}")
    print(f"Applied X-axis rotation: {format_number(args.rotate_x_deg, args.precision)} deg")
    print(f"Inventor x,y,z XLSX: {args.output}")
    print(f"Annotated normals XLSX: {args.normal_output}")
    if args.create_preview:
        print(f"Interactive preview HTML: {args.preview_output}")


if __name__ == "__main__":
    main()
