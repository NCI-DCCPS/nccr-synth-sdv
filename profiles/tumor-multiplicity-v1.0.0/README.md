# Patient-level tumor multiplicity v1.0.0

This profile replaces the one-or-two-tumor allocation in new all-source runs.
It pins the existing shared frozen snapshot's `analysis/quality.json`, whose
`tumor_rows_per_patient` histogram counts **patients**, not tumor rows or
lifetime sequence codes. The source contains 1,483,916 patients and 1,614,685
tumor rows, with observed multiplicities from 1 through 37 (some bins absent).
It is a file-derived STAGE_TEST_ONLY reference with unverified clinical
representativeness; it is not a public population estimate.

For each supported count k, the patient quota is N times its reference frequency.
The allocator rounds each quota down or up while preserving the requested
patient total and CTC/ABM tumor-row total exactly. Dynamic programming minimizes
absolute patient-count rounding error among these feasible allocations, with
deterministic support-order tie-breaking. Infeasible requests fail explicitly.
No unobserved multiplicity is invented. Rare bins can round to zero at small
scales. The source's observed maximum is evidence, not a hard-coded two-tumor cap.

A named seeded permutation assigns these counts to synthetic patients. This is
an explicit independence assumption: the histogram does not establish the joint
relationship between multiplicity, age, diagnosis, sex, or other attributes.
The new model selects distinct ordered diagnosis months before the existing
2022-12 cutoff. For patients needing more months than their drawn diagnosis year
allows, it resamples from the published year support conditioned on feasibility.
Birth-derived ages, follow-up, treatment windows, and MCE context remain linked.

Preflight archives the profile, frozen manifest and exact histogram artifact,
plus the rounded allocation. An independent verifier recounts actual CTC/ABM
rows, checks their patient/tumor keys and numberOfPrimaries, exact totals,
supported counts, and each bucket's rounding quota. Archived replay uses only
retained inputs and verifier code. Existing frozen recipes and outputs keep
their historical behavior.
