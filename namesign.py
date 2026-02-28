#!/usr/bin/env python3
"""
Name sign generator for two-color 3D printing.

Generates two STL files (black piece + white piece) for sequential printing
on a Prusa MK3S+ to produce a flush two-color mailbox name sign.

Printing workflow:
1. Print black STL first (thin layer with text + border shapes)
2. Leave on bed, swap to white filament
3. Print white STL on top - molten white bonds to black piece
4. Bottom surface shows black text/border flush with white background
"""

from dataclasses import dataclass, field
import argparse
import math
import sys

import cadquery as cq


CHAR_WIDTH_RATIO = 0.55  # Approximate average character width / font size


@dataclass
class StyledRun:
    """A run of text with uniform styling."""
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class SignParams:
    """Parameters for name sign generation."""

    lines: list[str] = field(default_factory=lambda: ["Her bor", "Ola Nordmann"])
    styled_lines: list[list[StyledRun]] | None = None  # Per-run styling (overrides lines + bold/italic/underline)
    sizes: list[float] | None = None  # Per-line font sizes (mm), None = auto
    width: float = 180.0
    height: float = 120.0
    thickness: float = 3.0
    text_depth: float = 0.6
    font: str = "Arial"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    border_style: str = "concave"  # "concave", "rounded", "none"
    corner_radius: float = 12.0
    border_offset: float = 6.0
    border_width: float = 2.0
    line_spacing: float = 1.3
    output: str = "namesign"


def _clamp_radius(r, w, h):
    """Clamp corner radius to fit within dimensions."""
    return max(0, min(r, w / 2 - 0.1, h / 2 - 0.1))


# ---------------------------------------------------------------------------
# 2D wire builders (for offset2D support)
# ---------------------------------------------------------------------------


def _create_concave_wire(w, h, r):
    """Create a 2D closed wire for a concave plaque outline."""
    r = _clamp_radius(r, w, h)
    if r < 0.1:
        return cq.Workplane("XY").rect(w, h)

    hw, hh = w / 2, h / 2
    k = r * math.cos(math.pi / 4)  # r * 0.7071

    return (
        cq.Workplane("XY")
        .moveTo(-hw + r, -hh)
        .lineTo(hw - r, -hh)
        .threePointArc((hw - k, -hh + k), (hw, -hh + r))
        .lineTo(hw, hh - r)
        .threePointArc((hw - k, hh - k), (hw - r, hh))
        .lineTo(-hw + r, hh)
        .threePointArc((-hw + k, hh - k), (-hw, hh - r))
        .lineTo(-hw, -hh + r)
        .threePointArc((-hw + k, -hh + k), (-hw + r, -hh))
        .close()
    )


def _create_rounded_wire(w, h, r):
    """Create a 2D closed wire for a rounded rectangle."""
    r = _clamp_radius(r, w, h)
    if r < 0.1:
        return cq.Workplane("XY").rect(w, h)

    hw, hh = w / 2, h / 2
    # a = distance from corner to arc midpoint along each axis
    a = r * (1 - math.cos(math.pi / 4))  # r * 0.2929

    return (
        cq.Workplane("XY")
        .moveTo(-hw + r, -hh)
        .lineTo(hw - r, -hh)
        .threePointArc((hw - a, -hh + a), (hw, -hh + r))
        .lineTo(hw, hh - r)
        .threePointArc((hw - a, hh - a), (hw - r, hh))
        .lineTo(-hw + r, hh)
        .threePointArc((-hw + a, hh - a), (-hw, hh - r))
        .lineTo(-hw, -hh + r)
        .threePointArc((-hw + a, -hh + a), (-hw + r, -hh))
        .close()
    )


def _create_outline_wire(w, h, r, style):
    """Create a 2D closed wire based on border style."""
    if style == "concave":
        return _create_concave_wire(w, h, r)
    elif style == "rounded":
        return _create_rounded_wire(w, h, r)
    else:
        return cq.Workplane("XY").rect(w, h)


# ---------------------------------------------------------------------------
# 3D solid builders
# ---------------------------------------------------------------------------


def _create_outline_solid(w, h, r, style, depth):
    """Create a plate outline solid based on border style."""
    return _create_outline_wire(w, h, r, style).extrude(depth)


def _create_border_frame(params):
    """Create the border frame solid using offset2D for constant distance.

    Returns None if border_style is 'none' or border_width <= 0.
    """
    if params.border_style == "none" or params.border_width <= 0:
        return None

    off = params.border_offset
    bw = params.border_width

    if off <= 0 and bw <= 0:
        return None

    try:
        # Create separate wires for each offset (CadQuery shares context
        # between wire/offset, so extrude on one consumes the other's state)
        wire1 = _create_outline_wire(
            params.width, params.height, params.corner_radius, params.border_style
        )
        outer_solid = wire1.offset2D(-off, kind="arc").extrude(params.text_depth)

        wire2 = _create_outline_wire(
            params.width, params.height, params.corner_radius, params.border_style
        )
        inner_solid = wire2.offset2D(-(off + bw), kind="arc").extrude(params.text_depth)

        return outer_solid.cut(inner_solid)
    except Exception as e:
        print(f"Warning: offset2D failed ({e}), using fallback border", file=sys.stderr)
        # Fallback: simple dimension-based approach
        outer_w = params.width - 2 * off
        outer_h = params.height - 2 * off
        outer_r = max(0, params.corner_radius - off)
        inner_w = outer_w - 2 * bw
        inner_h = outer_h - 2 * bw
        inner_r = max(0, outer_r - bw)
        if outer_w <= 0 or outer_h <= 0:
            return None
        outer_s = _create_outline_solid(
            outer_w, outer_h, outer_r, params.border_style, params.text_depth
        )
        if inner_w <= 0 or inner_h <= 0:
            return outer_s
        inner_s = _create_outline_solid(
            inner_w, inner_h, inner_r, params.border_style, params.text_depth
        )
        return outer_s.cut(inner_s)


def _get_line_texts(params):
    """Get plain text for each line, from styled_lines if available."""
    if params.styled_lines is not None:
        return ["".join(run.text for run in runs) for runs in params.styled_lines]
    return params.lines


def auto_font_sizes(params):
    """Calculate automatic font sizes - uniform across all lines.

    Computes the largest font size that fits all lines within the available
    space, then returns that same size for every line.
    """
    lines = _get_line_texts(params)
    n = len(lines)
    if n == 0:
        return []

    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return [10.0] * n

    # Determine available space inside border
    if params.border_style != "none" and params.border_width > 0:
        padding = params.border_offset + params.border_width + 3
    else:
        padding = max(params.corner_radius * 0.5, 4)

    available_w = params.width - 2 * padding
    available_h = params.height - 2 * padding

    if available_w <= 0 or available_h <= 0:
        return [5.0] * n

    # Base size from available height
    n_text = len(non_empty)
    base_size = available_h / (1 + (n_text - 1) * params.line_spacing)

    # Cap by the widest line so all lines use the same size
    uniform = base_size
    for line in lines:
        text = line.strip()
        if not text:
            continue
        max_w = available_w / (len(text) * CHAR_WIDTH_RATIO)
        uniform = min(uniform, max_w)

    return [uniform] * n


def _calc_line_positions(line_data, line_spacing):
    """Calculate centered Y positions for text lines.

    Uses per-line sizes so that spacing adapts to different font sizes:
    gap between line i and i+1 = (size_i/2 + size_{i+1}/2) * line_spacing

    Returns list of Y positions (top line first, positive Y = up).
    """
    n = len(line_data)
    if n == 0:
        return []
    if n == 1:
        return [0.0]

    # Compute gaps between consecutive line centers
    gaps = []
    for i in range(n - 1):
        gap = (line_data[i][1] / 2 + line_data[i + 1][1] / 2) * line_spacing
        gaps.append(gap)

    total_span = sum(gaps)
    y_positions = [total_span / 2]
    for gap in gaps:
        y_positions.append(y_positions[-1] - gap)

    return y_positions


def _find_font_path(font_name, bold=False, italic=False):
    """Try to find a system font file path for a given style combination.

    Returns the path string if found, or None.
    """
    import platform
    from pathlib import Path

    if platform.system() != "Windows":
        return None

    fonts_dir = Path("C:/Windows/Fonts")
    if not fonts_dir.exists():
        return None

    # Common font file naming conventions
    name_lower = font_name.lower().replace(" ", "")
    suffixes = []
    if bold and italic:
        suffixes = ["bi", "bolditalic", "z"]
    elif bold:
        suffixes = ["bd", "b", "bold"]
    elif italic:
        suffixes = ["i", "italic"]

    for suffix in suffixes:
        for ext in [".ttf", ".otf"]:
            candidate = fonts_dir / f"{name_lower}{suffix}{ext}"
            if candidate.exists():
                return str(candidate)
            # Try uppercase
            candidate = fonts_dir / f"{name_lower}{suffix}{ext}".upper()
            if candidate.exists():
                return str(candidate)

    # Case-insensitive search as last resort
    target_prefix = name_lower
    for f in fonts_dir.iterdir():
        fn = f.name.lower()
        if fn.startswith(target_prefix) and fn.endswith((".ttf", ".otf")):
            if bold and italic and any(s in fn for s in ["bi", "bolditalic"]):
                return str(f)
            elif bold and not italic and any(s in fn for s in ["bd", "bold"]):
                if "italic" not in fn and "bi" not in fn.replace("bold", ""):
                    return str(f)
            elif italic and not bold and ("italic" in fn or fn.endswith("i.ttf")):
                if "bold" not in fn and "bd" not in fn:
                    return str(f)

    return None


def _text_kwargs_for_style(font, bold, italic):
    """Build CadQuery text() kwargs for a given bold/italic combination."""
    kwargs = dict(halign="center", valign="center")
    if bold and italic:
        fp = _find_font_path(font, bold=True, italic=True)
        if fp:
            kwargs["fontPath"] = fp
        else:
            kwargs["font"] = font
            kwargs["kind"] = "bold"  # fallback: bold only
    elif bold:
        kwargs["font"] = font
        kwargs["kind"] = "bold"
    elif italic:
        kwargs["font"] = font
        kwargs["kind"] = "italic"
    else:
        kwargs["font"] = font
        kwargs["kind"] = "regular"
    return kwargs


def _create_text_solids(params):
    """Create extruded text solids for all lines.

    If styled_lines is set, creates per-run solids with individual styling.
    Otherwise uses global bold/italic/underline flags.

    Returns a CadQuery Workplane with combined text solids, or None if no text.
    """
    line_texts = _get_line_texts(params)
    non_empty_indices = [i for i, l in enumerate(line_texts) if l.strip()]
    if not non_empty_indices:
        return None

    sizes = (
        params.sizes
        if params.sizes and len(params.sizes) == len(line_texts)
        else auto_font_sizes(params)
    )

    # Build (text, size) pairs for non-empty lines
    line_data = [(line_texts[i].strip(), sizes[i] if i < len(sizes) else sizes[-1])
                 for i in non_empty_indices]

    y_positions = _calc_line_positions(line_data, params.line_spacing)

    # Underline sits at the baseline. Empirically measured: for CadQuery text
    # with valign="center", the baseline is at ~0.33 (Arial) to ~0.36 (Cambria)
    # of font_size below center. Using 0.35 as a cross-font compromise.
    UL_Y_OFFSET = 0.35  # fraction of font_size below center

    solids = []

    if params.styled_lines is not None:
        # Per-run styling: group consecutive runs by (bold, italic) into
        # text groups so each group is a single text() call.  Underlines
        # are drawn separately as rectangles (they aren't a font property).
        data_idx = 0
        for line_idx in non_empty_indices:
            runs = params.styled_lines[line_idx]
            font_size = line_data[data_idx][1]
            y = y_positions[data_idx]
            data_idx += 1

            # Merge consecutive runs with same (bold, italic) into text groups
            groups = []  # [(merged_text, bold, italic, [(run_text, underline), ...])]
            for run in runs:
                if not run.text:
                    continue
                key = (run.bold, run.italic)
                if groups and (groups[-1][1], groups[-1][2]) == key:
                    groups[-1][0] += run.text
                    groups[-1][3].append((run.text, run.underline))
                else:
                    groups.append([run.text, run.bold, run.italic,
                                   [(run.text, run.underline)]])

            if not groups:
                continue

            # If there's only one text group, render centered (no CHAR_WIDTH_RATIO error)
            if len(groups) == 1:
                g_text, g_bold, g_italic, g_runs = groups[0]
                kwargs = _text_kwargs_for_style(params.font, g_bold, g_italic)
                try:
                    solid = (
                        cq.Workplane("XY")
                        .center(0, y)
                        .text(g_text, font_size, params.text_depth, **kwargs)
                    )
                    solids.append(solid)
                except Exception as e:
                    print(f"Warning: Could not render text '{g_text}': {e}", file=sys.stderr)

                # Underline rectangles per sub-run
                full_w = len(g_text) * CHAR_WIDTH_RATIO * font_size
                char_x = -full_w / 2
                for sub_text, sub_ul in g_runs:
                    sub_w = len(sub_text) * CHAR_WIDTH_RATIO * font_size
                    if sub_ul:
                        ul_thickness = max(0.4, font_size * 0.06)
                        ul_y = y - font_size * UL_Y_OFFSET
                        ul = (
                            cq.Workplane("XY")
                            .center(char_x + sub_w / 2, ul_y)
                            .rect(sub_w, ul_thickness)
                            .extrude(params.text_depth)
                        )
                        solids.append(ul)
                    char_x += sub_w
            else:
                # Multiple font-style groups: position each using CHAR_WIDTH_RATIO
                full_text = "".join(g[0] for g in groups)
                total_w = len(full_text) * CHAR_WIDTH_RATIO * font_size
                x = -total_w / 2

                for g_text, g_bold, g_italic, g_runs in groups:
                    g_w = len(g_text) * CHAR_WIDTH_RATIO * font_size
                    g_cx = x + g_w / 2

                    kwargs = _text_kwargs_for_style(params.font, g_bold, g_italic)
                    try:
                        solid = (
                            cq.Workplane("XY")
                            .center(g_cx, y)
                            .text(g_text, font_size, params.text_depth, **kwargs)
                        )
                        solids.append(solid)
                    except Exception as e:
                        print(f"Warning: Could not render text '{g_text}': {e}", file=sys.stderr)

                    # Underline rectangles per sub-run within this group
                    char_x = x
                    for sub_text, sub_ul in g_runs:
                        sub_w = len(sub_text) * CHAR_WIDTH_RATIO * font_size
                        if sub_ul:
                            ul_thickness = max(0.4, font_size * 0.06)
                            ul_y = y - font_size * UL_Y_OFFSET
                            ul = (
                                cq.Workplane("XY")
                                .center(char_x + sub_w / 2, ul_y)
                                .rect(sub_w, ul_thickness)
                                .extrude(params.text_depth)
                            )
                            solids.append(ul)
                        char_x += sub_w

                    x += g_w
    else:
        # Global styling (CLI mode)
        text_kwargs = _text_kwargs_for_style(params.font, params.bold, params.italic)

        for i, (text, font_size) in enumerate(line_data):
            y = y_positions[i]
            try:
                solid = (
                    cq.Workplane("XY")
                    .center(0, y)
                    .text(text, font_size, params.text_depth, **text_kwargs)
                )
                solids.append(solid)
            except Exception as e:
                print(f"Warning: Could not render text '{text}': {e}", file=sys.stderr)

            if params.underline:
                text_w = font_size * len(text) * CHAR_WIDTH_RATIO
                ul_thickness = max(0.4, font_size * 0.06)
                ul_y = y - font_size * UL_Y_OFFSET
                ul = (
                    cq.Workplane("XY")
                    .center(0, ul_y)
                    .rect(text_w, ul_thickness)
                    .extrude(params.text_depth)
                )
                solids.append(ul)

    if not solids:
        return None

    combined = solids[0]
    for s in solids[1:]:
        combined = combined.union(s)

    return combined


def generate_sign(params):
    """Generate the two-piece name sign.

    Returns:
        (black_piece, white_piece): CadQuery Workplane objects.
        black_piece contains text + border (thin, printed first).
        white_piece is the full plate with voids for the black piece.
    """
    plate = _create_outline_solid(
        params.width, params.height, params.corner_radius,
        params.border_style, params.thickness,
    )

    text_solids = _create_text_solids(params)
    border = _create_border_frame(params)

    black_parts = []
    if text_solids is not None:
        black_parts.append(text_solids)
    if border is not None:
        black_parts.append(border)

    if not black_parts:
        return None, plate

    black_combined = black_parts[0]
    for part in black_parts[1:]:
        black_combined = black_combined.union(part)

    # Mirror across YZ plane so text reads correctly from bed side (z=0)
    black_piece = black_combined.mirror("YZ")

    # White piece = full plate minus the black piece voids
    white_piece = plate.cut(black_piece)

    return black_piece, white_piece


def export_stl(shape, filename):
    """Export a CadQuery shape to STL file."""
    cq.exporters.export(shape, filename)


def main():
    parser = argparse.ArgumentParser(
        description="Generate two-color 3D-printable name signs",
    )
    parser.add_argument("lines", nargs="+", help="Text lines for the sign")
    parser.add_argument(
        "--sizes", nargs="*", type=float, default=None,
        help="Font size per line (mm). Auto-calculated if not specified.",
    )
    parser.add_argument("--width", type=float, default=180, help="Sign width (mm)")
    parser.add_argument("--height", type=float, default=120, help="Sign height (mm)")
    parser.add_argument("--thickness", type=float, default=3.0, help="Total sign thickness (mm)")
    parser.add_argument("--text-depth", type=float, default=0.6, help="Black layer thickness (mm)")
    parser.add_argument("--font", default="Arial", help="Font name")
    parser.add_argument("--bold", action="store_true", help="Bold text")
    parser.add_argument("--italic", action="store_true", help="Italic text")
    parser.add_argument("--underline", action="store_true", help="Underline text")
    parser.add_argument(
        "--border-style", default="concave",
        choices=["concave", "rounded", "none"],
        help="Sign outline style",
    )
    parser.add_argument("--corner-radius", type=float, default=12, help="Corner radius (mm)")
    parser.add_argument("--border-offset", type=float, default=6, help="Edge to border distance (mm)")
    parser.add_argument("--border-width", type=float, default=2, help="Border line width (mm)")
    parser.add_argument("--line-spacing", type=float, default=1.3, help="Line spacing multiplier")
    parser.add_argument("-o", "--output", default="namesign", help="Output filename prefix")

    args = parser.parse_args()

    params = SignParams(
        lines=args.lines,
        sizes=args.sizes,
        width=args.width,
        height=args.height,
        thickness=args.thickness,
        text_depth=args.text_depth,
        font=args.font,
        bold=args.bold,
        italic=args.italic,
        underline=args.underline,
        border_style=args.border_style,
        corner_radius=args.corner_radius,
        border_offset=args.border_offset,
        border_width=args.border_width,
        line_spacing=args.line_spacing,
        output=args.output,
    )

    print(f"Generating sign: {' / '.join(params.lines)}")
    print(f"  Size: {params.width} x {params.height} x {params.thickness} mm")
    print(f"  Style: {params.border_style}, font: {params.font}")

    style_flags = []
    if params.bold:
        style_flags.append("bold")
    if params.italic:
        style_flags.append("italic")
    if params.underline:
        style_flags.append("underline")
    if style_flags:
        print(f"  Text style: {', '.join(style_flags)}")

    if params.sizes:
        print(f"  Font sizes: {params.sizes}")
    else:
        sizes = auto_font_sizes(params)
        print(f"  Auto font size: {sizes[0]:.1f} mm")

    black, white = generate_sign(params)

    black_file = f"{params.output}_black.stl"
    white_file = f"{params.output}_white.stl"

    if black is not None:
        export_stl(black, black_file)
        print(f"  Exported: {black_file}")
    else:
        print("  No black piece (no text or border)")

    export_stl(white, white_file)
    print(f"  Exported: {white_file}")
    print("Done!")


if __name__ == "__main__":
    main()
