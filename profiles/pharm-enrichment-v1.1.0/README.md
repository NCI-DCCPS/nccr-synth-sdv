# Integrated PHARM tuple profile v1.1.0

This version makes the scoped v1.0.0 PHARM allocation explicit for full generation.
It pins the existing RxNorm manifest and normalized tuples; rules v2.3.0 pin
CanMED and published PHARM frequencies. The all-source recipe pins this profile.
All lookup source/receipt files are hash-checked and archived offline.

Allocation preserves published CanMED category mass, then published RxNorm missing
mass. Within CanMED categories, reported generic-name weights are conditioned on
eligible lookup names. A category without a reported supported name retains its
published count and uses explicitly uniform distinct eligible names. RxNorm name
weights are conditioned on captured supported names. Largest remainder with
lexicographic tie breaking allocates exact group counts. Package selection is
uniform within each selected group, followed by a seeded row shuffle. Lookup
multiplicity never determines drug-group usage.

NDC=UNK is the explicit synthetic representation of the published RxNorm missing
mass; it is not an inferred observed NDC distribution. Tuple consistency and
field-specific missing/not-sought codes are mandatory. Unsupported and unreported
public mass, allocation precedence and resulting marginal deviations are retained.
Independent verification recomputes group counts and applies a familywise 0.001
Hoeffding bound to package allocation.

The temporal policy is identity-only. Current package status does not imply
historical availability or clinical appropriateness. Patient birth, coverage and
death limits are checked; package date comparisons and missing endpoints are
reported separately. Claims stay linked to CTC and confirmed MCE patients.
