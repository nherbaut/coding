"""Stable code colors and presentation-only passage highlighting."""

from copy import deepcopy
import colorsys
import hashlib
import re

HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def code_color(code):
    color = code.get("color")
    if isinstance(color, str) and HEX_COLOR.fullmatch(color):
        return color.lower()
    digest = hashlib.sha256(code["id"].encode()).digest()
    hue = int.from_bytes(digest[:2], "big") / 65536
    lightness = 0.58 + digest[3] / 255 * 0.10
    saturation = 0.60 + digest[2] / 255 * 0.18
    rgb = colorsys.hls_to_rgb(hue, lightness, saturation)
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)


def colored_book(book):
    result = deepcopy(book)
    for code in result["codes"]:
        if code["kind"] == "code":
            code["color"] = code_color(code)
    return result


def highlight_style(colors):
    # A white tint preserves readable dark text, including user-selected dark colors.
    colors = list(dict.fromkeys(colors))
    if not colors:
        return ""
    stops = []
    for index, color in enumerate(colors):
        tint = f"color-mix(in srgb, {color} 40%, white)"
        stops.extend(
            (
                f"{tint} {index * 100 / len(colors):g}%",
                f"{tint} {(index + 1) * 100 / len(colors):g}%",
            )
        )
    return (
        "background:linear-gradient(to bottom," + ",".join(stops) + ");color:#24332e;"
    )


def highlighted_segments(text, annotations, codes):
    encoded = text.encode("utf-16-le")
    points = sorted(
        {
            0,
            len(encoded) // 2,
            *(p for a in annotations for p in (a["start"], a["end"])),
        }
    )
    lookup = {code["id"]: code for code in codes}
    segments = []
    for start, end in zip(points, points[1:]):
        ids = sorted(
            {
                identity
                for a in annotations
                if a["start"] < end and a["end"] > start
                for identity in a["codes"]
            }
        )
        colors = [code_color(lookup[identity]) for identity in ids]
        segments.append(
            dict(
                text=encoded[start * 2 : end * 2].decode("utf-16-le"),
                style=highlight_style(colors),
                title=", ".join(lookup[identity]["name"] for identity in ids),
            )
        )
    return segments
