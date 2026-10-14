from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import StrEnum

AMOUNT_QUANTUM = Decimal("0.01")
PRICE_QUANTUM = Decimal("0.00000001")
QUANTITY_QUANTUM = Decimal("0.000000000001")
PERCENT_QUANTUM = Decimal("0.000001")


class DecimalKind(StrEnum):
    AMOUNT = "amount"
    PRICE = "price"
    QUANTITY = "quantity"
    PERCENT = "percentage"


_QUANTA = {
    DecimalKind.AMOUNT: AMOUNT_QUANTUM,
    DecimalKind.PRICE: PRICE_QUANTUM,
    DecimalKind.QUANTITY: QUANTITY_QUANTUM,
    DecimalKind.PERCENT: PERCENT_QUANTUM,
}


def canonical_decimal(
    value: str | int | Decimal,
    kind: DecimalKind,
    *,
    allow_negative: bool = True,
) -> str:
    """Return a fixed-scale decimal string using round-half-to-even.

    Floats are deliberately excluded so binary floating-point can never enter a
    stored financial value through this boundary.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("Financial values must be supplied as decimal strings")
    try:
        parsed = Decimal(value.strip() if isinstance(value, str) else value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Financial value is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError("Financial value must be finite")
    if not allow_negative and parsed < 0:
        raise ValueError("Financial value cannot be negative")
    quantum = _QUANTA[kind]
    try:
        with localcontext() as context:
            context.prec = 80
            rounded = parsed.quantize(quantum, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise ValueError("Financial value exceeds supported precision") from exc
    if rounded == 0:
        rounded = abs(rounded)
    return format(rounded, "f")
