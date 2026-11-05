from pathlib import Path
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

TZ = "Europe/Helsinki"

# Simulation window: 12 Nov 12:00 to 13 Nov 24:00 (i.e. 14 Nov 00:00)
SIM_START = pd.Timestamp("2026-11-12 12:00", tz=TZ)
SIM_END = pd.Timestamp("2026-11-14 00:00", tz=TZ)

SLOT_MINUTES = 15
SLOT = pd.Timedelta(minutes=SLOT_MINUTES)
SLOT_HOURS = SLOT_MINUTES / 60.0
HORIZON = 48

SITES = ["S1", "S2"]

FAN_RPM_THRESHOLD = 550

# Used by module 4 (real health); the dummy health in module 1 does not use it
INJECTED_OUTAGES = {
    "S1": [
        (
            pd.Timestamp("2026-11-13 01:00", tz=TZ),
            pd.Timestamp("2026-11-13 03:00", tz=TZ),
        )
    ]
}

LOG_PATH = OUTPUT_DIR / "log.csv"