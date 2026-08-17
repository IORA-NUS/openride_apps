"""Generalized OO sampler hierarchy (plan §3.2.1).

Every ``Distribution`` shares ``sample(rng)``, so policies are agnostic to which
they were handed. The "trip matrix" is a :class:`ProbabilityMatrix`; historical
data (:class:`EmpiricalData`) can be *extended into* a matrix.
"""

from .base import Distribution
from .categorical import Categorical, Uniform
from .curve import Curve
from .empirical import EmpiricalData
from .matrix import ProbabilityMatrix
from .registry import DISTRIBUTION_REGISTRY, known_distributions, register
from .sources import load_curve_file, load_matrix_file, load_records_file

__all__ = [
    "Distribution",
    "Uniform",
    "Categorical",
    "ProbabilityMatrix",
    "Curve",
    "EmpiricalData",
    "DISTRIBUTION_REGISTRY",
    "known_distributions",
    "register",
    "load_matrix_file",
    "load_curve_file",
    "load_records_file",
]
