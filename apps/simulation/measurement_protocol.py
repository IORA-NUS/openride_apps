"""Measurement protocol R0-R7 as an executable check, not prose.

`docs/cooperation_experiment_design.md` §8 / plan §14.3. This exists because the
round-2 review found the shared-pool gate quoting a **+0.583 pp** effect whose own
*control arm* spanned **1.00 pp** across six runs. Every correctness item in the gate
passed; none of them looked at whether the instrument could see the effect.

> **R2 — Run the control arm's replicates in the same batch, interleaved, and report
> the control's own spread beside the effect. If control spread >= effect, the
> run-level comparison is NOT resolvable and must not be quoted.**

The one rule that would have caught it, so it is the one made mechanical here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

#: Two-sample size for 80% power at alpha=0.05:
#:     n per arm = 2 * (z_{a/2} + z_b)^2 * (sd/effect)^2 = 2 * 2.8^2 * (sd/effect)^2
#: The leading 2 is the TWO-SAMPLE factor and is easy to drop — doing so understated
#: the requirement by ~2x (it returned n=3 where the correct answer is n=8, i.e. it
#: would have certified exactly the under-powered comparison this module exists to
#: refuse). Kept as named constants so the arithmetic is auditable, not folklore.
_Z_SUM = 2.8
_TWO_SAMPLE_FACTOR = 2


@dataclass
class ArmSummary:
    name: str
    values: List[float]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> Optional[float]:
        return (sum(self.values) / self.n) if self.n else None

    @property
    def spread(self) -> Optional[float]:
        """max - min. Deliberately the RANGE, not a CI: with n=3 a CI is theatre."""
        return (max(self.values) - min(self.values)) if self.n else None

    @property
    def sd(self) -> Optional[float]:
        if self.n < 2:
            return None
        m = self.mean
        return math.sqrt(sum((v - m) ** 2 for v in self.values) / (self.n - 1))


@dataclass
class Resolvability:
    """Whether a run-level contrast may be quoted at all."""

    effect: Optional[float]
    control_spread: Optional[float]
    control_sd: Optional[float]
    required_n: Optional[int]
    resolvable: bool
    reasons: List[str] = field(default_factory=list)

    def report(self) -> str:
        head = "RESOLVABLE" if self.resolvable else "NOT RESOLVABLE — do not quote this effect"
        lines = [head]
        if self.effect is not None:
            lines.append(f"  effect          = {self.effect:+.4f}")
        if self.control_spread is not None:
            lines.append(f"  control spread  = {self.control_spread:.4f} (range, same batch)")
        if self.control_sd is not None:
            lines.append(f"  control sd      = {self.control_sd:.4f}")
        if self.required_n is not None:
            lines.append(f"  required n/arm  = {self.required_n} (unpaired, 80% power, a=0.05)")
        lines.extend(f"  ! {r}" for r in self.reasons)
        return "\n".join(lines)


def assess(
    treatment: Sequence[float],
    baseline: Sequence[float],
    control: Sequence[float],
    *,
    min_n: int = 8,
) -> Resolvability:
    """Apply R1/R2/R7 to one contrast.

    ``control`` is the arm that differs from ``treatment`` in NOTHING that should
    matter (here: the pooled arm with zero pools). Its spread is the instrument's
    own noise, measured in the same batch — never inherited from another scenario
    (R3), because that inheritance is precisely what went wrong.
    """
    t, b, c = ArmSummary("treatment", list(treatment)), ArmSummary("baseline", list(baseline)), \
        ArmSummary("control", list(control))

    reasons: List[str] = []
    effect = (t.mean - b.mean) if (t.mean is not None and b.mean is not None) else None
    resolvable = True

    if c.n < 2:
        resolvable = False
        reasons.append("control arm has fewer than 2 replicates: its spread is unmeasured (R2)")
    if effect is None:
        resolvable = False
        reasons.append("an arm is empty: no effect to assess")

    if effect is not None and c.spread is not None:
        # THE rule. An effect smaller than the control's own spread is inside the
        # instrument's noise, whatever the means say.
        if c.spread >= abs(effect):
            resolvable = False
            reasons.append(
                f"control spread {c.spread:.4f} >= |effect| {abs(effect):.4f} "
                f"— the instrument's noise exceeds the signal (R2)"
            )

    required_n = None
    if effect is not None and c.sd and abs(effect) > 0:
        required_n = max(
            2, math.ceil(_TWO_SAMPLE_FACTOR * (_Z_SUM * c.sd / abs(effect)) ** 2)
        )
        if min(t.n, b.n) < required_n:
            resolvable = False
            reasons.append(
                f"n={min(t.n, b.n)} per arm is below the required {required_n} (R7); "
                f"use the paired estimator or pay for more replicates"
            )
    if min(t.n, b.n) < min_n:
        reasons.append(
            f"n={min(t.n, b.n)} is below the standing unpaired floor of {min_n} for "
            f"this scenario class (R7)"
        )

    return Resolvability(
        effect=effect,
        control_spread=c.spread,
        control_sd=c.sd,
        required_n=required_n,
        resolvable=resolvable,
        reasons=reasons,
    )


def paired_bound_caveat(a_only: int, b_only: int, common: int) -> str:
    """The sentence that must ship with every paired figure.

    The paired estimator conditions on a POST-TREATMENT variable: the common-order
    set is chosen by both arms' behaviour, so it is a collider, not a random
    subsample. The bias direction is knowable and favourable — if B declines orders
    it would have served badly, the common set flatters B — so the paired figure is
    a **conservative lower bound on B's penalty**, never an unbiased point estimate.
    """
    return (
        f"PAIRED FIGURE IS A LOWER BOUND, not a point estimate: common={common}, "
        f"A-only={a_only}, B-only={b_only}. Selection into the common set is "
        f"post-treatment (a collider); the bias flatters the pooled arm, so the true "
        f"penalty is at least this large."
    )


__all__ = ["ArmSummary", "Resolvability", "assess", "paired_bound_caveat"]
