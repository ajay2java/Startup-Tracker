"""US state layout data for the map — centroids for dot placement.

Coordinates are bounding-box centers of each state's path in
``static/us-map.svg`` (in that SVG's own 959x593 coordinate space), computed
once at build time from the actual path geometry rather than looked up from
an external geographic dataset — good enough for placing a small marker, not
meant to be a true geographic centroid.

The SVG itself is "Blank US Map (states only).svg" by Heitordp / Kaldari /
Jamesy0627144 (Wikimedia Commons), released under CC0 1.0 Universal.
"""
from __future__ import annotations

STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AK": (114.0, 510.2),
    "AL": (654.2, 415.5),
    "AR": (548.8, 374.3),
    "AZ": (194.4, 366.0),
    "CA": (84.3, 267.8),
    "CO": (317.3, 273.0),
    "CT": (858.8, 179.9),
    "DC": (801.6, 252.1),
    "DE": (827.1, 241.9),
    "FL": (718.1, 511.6),
    "GA": (714.2, 404.8),
    "HI": (284.1, 546.7),
    "IA": (523.4, 215.1),
    "ID": (193.0, 111.7),
    "IL": (590.5, 260.8),
    "IN": (644.2, 256.7),
    "KS": (439.5, 291.3),
    "KY": (658.1, 301.0),
    "LA": (566.1, 456.2),
    "MA": (873.7, 159.4),
    "MD": (796.9, 249.9),
    "ME": (895.2, 87.5),
    "MI": (631.9, 144.1),
    "MN": (520.2, 117.8),
    "MO": (542.9, 295.3),
    "MS": (594.0, 419.4),
    "MT": (273.2, 87.1),
    "NC": (766.8, 333.5),
    "ND": (414.7, 92.3),
    "NE": (419.6, 223.5),
    "NH": (867.4, 122.2),
    "NJ": (834.1, 217.4),
    "NM": (297.2, 374.1),
    "NV": (133.1, 252.3),
    "NY": (809.2, 157.3),
    "OH": (700.1, 237.3),
    "OK": (433.0, 361.4),
    "OR": (96.8, 118.6),
    "PA": (782.7, 212.0),
    "RI": (877.7, 173.2),
    "SC": (752.1, 380.2),
    "SD": (412.6, 163.7),
    "TN": (657.0, 342.0),
    "TX": (404.6, 452.5),
    "UT": (216.4, 249.4),
    "VA": (766.5, 282.9),
    "VT": (845.3, 127.7),
    "WA": (116.0, 48.5),
    "WI": (575.7, 151.5),
    "WV": (748.7, 264.2),
    "WY": (294.2, 181.1),
}

# Dot offset, in SVG units, applied to every centroid: "up-and-right, to
# clear the state abbreviation" (spec §5).
DOT_OFFSET = (6, -10)

# Too small to hold a dot without bleeding onto neighbors (spec §5) — these
# get a labeled box in a vertical column instead, off to the side of the map.
SMALL_NORTHEAST_STATES = ("RI", "DE", "CT", "NJ", "MA", "MD", "DC")

US_STATES = tuple(sorted(STATE_CENTROIDS))
