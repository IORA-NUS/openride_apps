"""Footprint "masks" are optional and per-code: a facility only gets a footprint
when its code declares a ``mask_file`` (in metadata) that actually exists. Codes
with no configured mask — e.g. CU/warehouse, which has no real footprint data —
get no footprint at all. This is the data-driven, never-compulsory contract."""

import json
import random
from collections import Counter

from apps.container_logistics.datagen import CodeRegistry, LocationCatalog
from apps.container_logistics.datagen.facility_masks import FacilityMaskIndex


def _write_csv(path):
    rows = [
        ("CT", 103.70, 1.26), ("CT", 103.72, 1.27),
        ("CU", 103.85, 1.30), ("CU", 103.86, 1.29), ("CU", 103.84, 1.31),
        ("MT", 103.78, 1.33), ("MT", 103.80, 1.34),
    ]
    with open(path, "w") as fp:
        fp.write("postal_code,code,address,lon,lat\n")
        for i, (code, lon, lat) in enumerate(rows):
            fp.write(f"{i:05d},{code},addr {i},{lon},{lat}\n")
    return str(path)


def _write_square_region(path, codes_lonlat, *, half=0.02):
    """A region file with one square polygon covering each given point."""
    feats = []
    for lon, lat in codes_lonlat:
        ring = [
            [lon - half, lat - half], [lon + half, lat - half],
            [lon + half, lat + half], [lon - half, lat + half],
            [lon - half, lat - half],
        ]
        feats.append({"type": "Feature", "properties": {},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    with open(path, "w") as fp:
        json.dump({"type": "FeatureCollection", "features": feats}, fp)


def test_mask_files_only_lists_codes_with_a_mask_file():
    registry = CodeRegistry.build(
        ["CT", "CU", "MT"], None,
        metadata={
            "CT": {"mask_file": "port_regions.geojson"},
            "CU": {"label": "Warehouse"},          # no mask_file
            "MT": {"mask_file": "depot_regions.geojson"},
        },
    )
    assert registry.mask_files() == {
        "CT": "port_regions.geojson",
        "MT": "depot_regions.geojson",
    }
    # CU declares no mask -> never gets a footprint, whatever the coordinates.
    idx = FacilityMaskIndex(registry.mask_files())
    assert idx.lookup("CU", 103.85, 1.30) is None
    # Unknown code -> None. Empty mapping (default) -> masks disabled.
    assert idx.lookup("XX", 103.85, 1.30) is None
    assert FacilityMaskIndex().lookup("CT", 103.70, 1.26) is None


def test_facility_sites_attach_footprint_only_for_masked_codes(tmp_path):
    csv = _write_csv(tmp_path / "locations.csv")
    catalog = LocationCatalog(csv)

    # Only CT and MT have a (present) mask file; CU has none.
    _write_square_region(tmp_path / "port.geojson", [(103.70, 1.26), (103.72, 1.27)])
    _write_square_region(tmp_path / "depot.geojson", [(103.78, 1.33), (103.80, 1.34)])
    registry = CodeRegistry.build(
        catalog.codes(), None,
        metadata={
            "CT": {"mask_file": "port.geojson"},
            "CU": {"label": "Warehouse"},
            "MT": {"mask_file": "depot.geojson"},
        },
    )

    # Point the index at our temp region files.
    masks = FacilityMaskIndex(registry.mask_files(), base_dir=str(tmp_path))

    counts = Counter()
    have_fp = Counter()
    rng = random.Random(0)
    for code in registry.codes:
        for lon, lat in catalog._by_code.get(code, []):
            counts[code] += 1
            if masks.lookup(code, lon, lat) is not None:
                have_fp[code] += 1

    # CT and MT addresses sit inside their squares -> footprints; CU never does.
    assert have_fp["CT"] == counts["CT"] and counts["CT"] > 0
    assert have_fp["MT"] == counts["MT"] and counts["MT"] > 0
    assert have_fp["CU"] == 0 and counts["CU"] > 0
