from __future__ import annotations

from decimal import Decimal, ROUND_DOWN


def money_string(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return f"{quantized:.2f}"
