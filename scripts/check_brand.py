#!/usr/bin/env python3
"""Fail if the retired product name returns or the central brand drifts."""
from __future__ import annotations

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
BRAND_FILE = APP_ROOT / "site" / "brand.js"


def main() -> int:
    brand_source = BRAND_FILE.read_text(encoding="utf-8")
    match = re.search(
        r'export\s+const\s+BRAND\s*=\s*Object\.freeze\(\{\s*name:\s*"([^"]+)"',
        brand_source,
        flags=re.MULTILINE,
    )
    if not match or match.group(1) != "Deadline Compass":
        raise SystemExit("Central brand display name is missing or changed")
    print("Brand check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
