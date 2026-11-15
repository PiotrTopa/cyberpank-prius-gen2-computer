"""
The alert functions (one module each). ``default_functions`` builds the set the
backend runs; every type in it ships in mode ``shadow`` (FEATURES.md lists
them with status, inputs and gaps).
"""

from __future__ import annotations

from typing import List

from ..model import AlertFunction


def default_functions(tuning, settings) -> List[AlertFunction]:
    fns: List[AlertFunction] = []
    for factory in _FACTORIES:
        fns.append(factory(tuning, settings))
    return fns


# (tuning, settings) -> AlertFunction, in priority-irrelevant order
_FACTORIES: list = []
