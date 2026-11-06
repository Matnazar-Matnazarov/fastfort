"""The map editor's WKT reader, run under Node from the file that ships.

`readWkt` in `fastfort-geo.js` is what turns the text box beside a geometry
field into something the map can draw. A shape it cannot read is not an error
anywhere: the map simply draws nothing and opens on the whole world -- which is
how the demo's MULTIPOINT "Stops" field sat on an empty map of the planet while
the route above it drew fine. These tests read the shipped function rather than
a Python copy of it, so they fail when the file does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "fastfort/ui/static/js/fastfort-geo.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


def read_wkt(text: str) -> Any:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("  const KINDS = {")
    end = source.index("\n  };\n", source.index("  const readWkt = (raw) => {")) + len("\n  };\n")
    program = (
        source[start:end] + f"process.stdout.write(JSON.stringify(readWkt({json.dumps(text)})));"
    )
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True, timeout=20
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "text",
    [
        # The form PostGIS writes, and the one `admin/geo.py` renders.
        "SRID=4326;MULTIPOINT((69.2035 41.3389), (69.2404 41.3502))",
        # The older, unbracketed form, which the standard also allows.
        "MULTIPOINT(69.2035 41.3389, 69.2404 41.3502)",
    ],
)
def test_both_multipoint_spellings_are_read(text: str) -> None:
    assert read_wkt(text) == {
        "type": "MultiPoint",
        "coordinates": [[69.2035, 41.3389], [69.2404, 41.3502]],
    }


def test_a_linestring_still_reads_as_before() -> None:
    shape = read_wkt("SRID=4326;LINESTRING(69.2 41.3, 69.3 41.2)")

    assert shape == {"type": "LineString", "coordinates": [[69.2, 41.3], [69.3, 41.2]]}


def test_a_polygon_keeps_its_rings_apart() -> None:
    shape = read_wkt("POLYGON((0 0, 4 0, 4 4, 0 0), (1 1, 2 1, 2 2, 1 1))")

    assert shape["type"] == "Polygon"
    assert len(shape["coordinates"]) == 2


def test_nonsense_is_nothing_rather_than_a_crash() -> None:
    assert read_wkt("not a shape") is None


def test_a_multipoint_has_a_dot_for_every_member() -> None:
    """A MULTIPOINT has no outline, so without its own dots the map centred on
    the stops and then drew none of them."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("  const looseDots = (shape) => {")
    end = source.index("\n  };\n", start) + len("\n  };\n")
    shape = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]},
            {"type": "Point", "coordinates": [5, 6]},
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        ],
    }
    program = (
        source[start:end] + f"process.stdout.write(JSON.stringify(looseDots({json.dumps(shape)})));"
    )
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True, timeout=20
    )

    assert json.loads(result.stdout) == [[1, 2], [3, 4], [5, 6]]
