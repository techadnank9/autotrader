from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_trader.brightdata_api import collect_stock_evidence, save_evidence_file
from ai_trader.config import Settings
from ai_trader.engine import BrightDataClient, DEFAULT_UNIVERSE


def main() -> None:
    settings = Settings.load()
    client = BrightDataClient(settings)
    symbols = DEFAULT_UNIVERSE[:4]
    payload = collect_stock_evidence(
        symbols,
        fetcher=lambda query, source: client._discover(query, source=source),
    )
    output_path = Path(__file__).resolve().with_name("stock_evidence.json")
    save_evidence_file(payload, output_path)
    print(f"Saved Bright Data evidence for {', '.join(symbols)} to {output_path}")


if __name__ == "__main__":
    main()
