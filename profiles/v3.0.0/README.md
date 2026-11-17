# Integrated all-source sampler profiles v3.0.0

These profiles preserve the prior field/context choices while assigning PHARM's
13 drug fields to complete lookup tuples and CCDI resource names to the shared
catalog. They are selected by the schema-4 `all-sources-integrated-v2.json` recipe,
which pins rules v2.3.0, catalog profile v1.0.0 and PHARM profile v1.1.0.
The profile version is independent of rule and dataset versions.

PHARM tuples are applied while writing claim rows. Patient IDs, quantities,
durations and dispensing offsets come from the same all-source generator; no
baseline dataset or separate repair step is required. The implementation retains
a shuffled vector of tuple references, not a second materialized claim table.
CCDI's processed mapping rows and CPI share the same generated triples. Raw CCDI
is serialized as one patient row with numbered mapping pairs.

Generation archives the complete effective PHARM plan, catalog conditioning,
small inputs and code. Independent checks recompute relationships and effective
frequencies from pinned references. No patient-level training data or live lookup
queries are used. Clinical suitability and historical package availability remain
outside the model's claims.
