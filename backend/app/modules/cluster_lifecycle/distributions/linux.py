"""Shared Linux preparation fragments used by SSH distribution installers."""

from functools import cache
from importlib import resources


@cache
def swap_script() -> str:
    return (resources.files(__package__) / "scripts" / "swap.sh").read_text()
