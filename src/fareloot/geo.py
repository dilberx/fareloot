"""Where airports are, so the watcher can tell a short hop from a long haul.

Provider distances are patchy (several endpoints omit them), and the decisions
that need distance are made before any quote exists: which origins are worth a
train ride for a destination, and how long a trip is worth committing to.
Coordinates are approximate; a few kilometres either way changes nothing here.
Unknown codes return None, and every caller treats None as "can't judge".
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

COORDS: dict[str, tuple[float, float]] = {
    # Kerala and nearby departure airports
    "COK": (10.15, 76.40),
    "TRV": (8.48, 76.92),
    "CCJ": (11.14, 75.95),
    "CNN": (11.92, 75.55),
    "CJB": (11.03, 77.04),
    "IXM": (9.83, 78.09),
    "IXE": (12.96, 74.89),
    "BLR": (13.20, 77.71),
    "MAA": (12.99, 80.17),
    "TRZ": (10.77, 78.71),
    "VGA": (16.53, 80.80),
    # Rest of India
    "BOM": (19.09, 72.87),
    "DEL": (28.56, 77.10),
    "GOI": (15.38, 73.83),
    "GOX": (15.73, 73.87),
    "HYD": (17.24, 78.43),
    "CCU": (22.65, 88.45),
    "AMD": (23.07, 72.63),
    "PNQ": (18.58, 73.92),
    "JAI": (26.82, 75.81),
    "LKO": (26.76, 80.89),
    "IXZ": (11.64, 92.73),
    "SXR": (33.99, 74.77),
    "IXL": (34.14, 77.55),
    "IXB": (26.68, 88.33),
    "GAU": (26.11, 91.59),
    "VNS": (25.45, 82.86),
    "ATQ": (31.71, 74.80),
    "IXC": (30.67, 76.79),
    "BBI": (20.24, 85.82),
    # Gulf
    "DXB": (25.25, 55.36),
    "DWC": (24.90, 55.16),
    "AUH": (24.43, 54.65),
    "SHJ": (25.33, 55.52),
    "RKT": (25.61, 55.94),
    "DOH": (25.27, 51.61),
    "MCT": (23.59, 58.28),
    "BAH": (26.27, 50.63),
    "KWI": (29.24, 47.97),
    "RUH": (24.96, 46.70),
    "JED": (21.68, 39.16),
    "DMM": (26.47, 49.80),
    "SLL": (17.04, 54.09),
    # South Asia
    "CMB": (7.18, 79.88),
    "MLE": (4.19, 73.53),
    "KTM": (27.70, 85.36),
    "DAC": (23.84, 90.40),
    "PBH": (27.40, 89.42),
    # Southeast Asia
    "BKK": (13.69, 100.75),
    "DMK": (13.91, 100.61),
    "HKT": (8.11, 98.32),
    "KBV": (8.10, 98.99),
    "USM": (9.55, 100.06),
    "CNX": (18.77, 98.96),
    "KUL": (2.75, 101.71),
    "PEN": (5.30, 100.28),
    "LGK": (6.33, 99.73),
    "SIN": (1.36, 103.99),
    "CGK": (-6.13, 106.66),
    "JKT": (-6.20, 106.85),
    "DPS": (-8.75, 115.17),
    "SGN": (10.82, 106.66),
    "HAN": (21.22, 105.81),
    "DAD": (16.04, 108.20),
    "PQC": (10.17, 103.99),
    "MNL": (14.51, 121.02),
    "CEB": (10.31, 123.98),
    "PNH": (11.55, 104.84),
    "REP": (13.41, 103.81),
    "RGN": (16.91, 96.13),
    # East Asia
    "HKG": (22.31, 113.91),
    "MFM": (22.15, 113.59),
    "TPE": (25.08, 121.23),
    "ICN": (37.46, 126.44),
    "SEL": (37.57, 126.98),
    "NRT": (35.77, 140.39),
    "HND": (35.55, 139.78),
    "TYO": (35.68, 139.69),
    "KIX": (34.43, 135.24),
    "OSA": (34.69, 135.50),
    "PVG": (31.14, 121.81),
    "SHA": (31.20, 121.34),
    "PEK": (40.08, 116.58),
    "BJS": (39.90, 116.40),
    "CAN": (23.39, 113.30),
    # Caucasus and Central Asia
    "GYD": (40.47, 50.05),
    "BAK": (40.41, 49.87),
    "TBS": (41.67, 44.95),
    "EVN": (40.15, 44.40),
    "ALA": (43.35, 77.04),
    "NQZ": (51.02, 71.47),
    "TAS": (41.26, 69.28),
    "SKD": (39.70, 66.98),
    # Europe and nearby
    "IST": (41.26, 28.74),
    "SAW": (40.90, 29.31),
    "AYT": (36.90, 30.80),
    "LHR": (51.47, -0.45),
    "LON": (51.51, -0.13),
    "LGW": (51.15, -0.18),
    "CDG": (49.01, 2.55),
    "PAR": (48.86, 2.35),
    "FRA": (50.03, 8.57),
    "MUC": (48.35, 11.79),
    "AMS": (52.31, 4.76),
    "MXP": (45.63, 8.72),
    "MIL": (45.46, 9.19),
    "FCO": (41.80, 12.25),
    "ROM": (41.90, 12.50),
    "BCN": (41.30, 2.08),
    "MAD": (40.47, -3.56),
    "ATH": (37.94, 23.94),
    "VIE": (48.11, 16.57),
    "ZRH": (47.46, 8.55),
    "PRG": (50.10, 14.26),
    "BUD": (47.44, 19.26),
    "WAW": (52.17, 20.97),
    "LIS": (38.77, -9.13),
    "DUB": (53.42, -6.27),
    "HEL": (60.32, 24.96),
    "CPH": (55.62, 12.65),
    "MOW": (55.76, 37.62),
    "SVO": (55.97, 37.41),
    "LED": (59.80, 30.26),
    # Africa
    "CAI": (30.12, 31.41),
    "NBO": (-1.32, 36.93),
    "ADD": (8.98, 38.80),
    "JNB": (-26.14, 28.24),
    "CPT": (-33.97, 18.60),
    "SEZ": (-4.67, 55.52),
    "MRU": (-20.43, 57.68),
    "ZNZ": (-6.22, 39.22),
    "DAR": (-6.88, 39.20),
    "CMN": (33.37, -7.59),
    # Oceania and the Americas
    "SYD": (-33.95, 151.18),
    "MEL": (-37.67, 144.84),
    "PER": (-31.94, 115.97),
    "AKL": (-37.01, 174.79),
    "JFK": (40.64, -73.78),
    "NYC": (40.71, -74.01),
    "EWR": (40.69, -74.17),
    "ORD": (41.97, -87.91),
    "SFO": (37.62, -122.38),
    "LAX": (33.94, -118.41),
    "YYZ": (43.68, -79.63),
    "YVR": (49.19, -123.18),
    "YUL": (45.47, -73.74),
    "YMQ": (45.50, -73.57),
}

EARTH_RADIUS_KM = 6371.0


def km(a: str, b: str) -> int | None:
    """Great-circle distance between two airport or city codes, or None."""
    pa, pb = COORDS.get(a.upper()), COORDS.get(b.upper())
    if pa is None or pb is None:
        return None
    lat1, lon1, lat2, lon2 = map(radians, (*pa, *pb))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return int(2 * EARTH_RADIUS_KM * asin(sqrt(h)))
