"""Kubernetes resource quantity parsing (CPU millicores, memory bytes)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_BINARY: dict[str, int] = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal(10**3),
    "M": Decimal(10**6),
    "G": Decimal(10**9),
    "T": Decimal(10**12),
    "P": Decimal(10**15),
    "E": Decimal(10**18),
}


def _parse(value: str | int | float | None) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    text = str(value).strip()
    try:
        for suffix, factor in _BINARY.items():
            if text.endswith(suffix):
                return Decimal(text[: -len(suffix)]) * factor
        for dsuffix, dfactor in _DECIMAL.items():
            if text.endswith(dsuffix):
                return Decimal(text[: -len(dsuffix)]) * dfactor
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)


def cpu_millicores(value: str | int | float | None) -> int:
    """'250m' -> 250, '2' -> 2000, '1.5' -> 1500, '123456n' -> 0 (rounded)."""
    return int((_parse(value) * 1000).to_integral_value())


def memory_bytes(value: str | int | float | None) -> int:
    """'128Mi' -> 134217728, '1G' -> 1000000000, '1024' -> 1024."""
    return int(_parse(value).to_integral_value())
