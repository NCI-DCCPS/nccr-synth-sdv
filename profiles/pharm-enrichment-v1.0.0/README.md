# Scoped PHARM tuple enrichment v1.0.0

Select an existing SDV PHARM payload with blank drug fields. Preserve its header,
metadata and every row's patient ID, dispensing quantity/duration and offset.
Other source files remain byte-identical. This postprocessor does not fit a model
or alter existing full-generation recipes.

`scripts/regenerate_pharm.py RECIPE.json` consumes explicit SHA-256 pins for the
baseline release, original PHARM, v2.2.0 rules, frozen public report/normalized
frequencies, CanMED and RxNorm lookup manifests/tuples, metadata and patient context.
Outputs are new experiment-local directories and a candidate manifest. Existing
destinations are refused. It never promotes or uploads. An effective plan with all
group counts, tuples and policies is archived before writing the candidate.

Allocation priority:

1. Preserve full published CanMED category counts proportionally, including no
   CanMED match, using deterministic largest-remainder allocation.
2. Preserve published RxNorm display-name missingness proportionally. `UNK` NDC
   is an explicit synthetic assumption for those unresolved rows, not an observed
   source NDC. All associated missing codes follow metadata.
3. Within each CanMED category, condition published generic-name weights on exact
   case-insensitive lookup name support. Keep authoritative source spelling in
   output. If no reported name supports a category, allocate its count uniformly
   among distinct supported names and disclose that synthetic assumption.
4. For RxNorm matches, condition reported display-name weights on supported exact
   names. Unreported tail mass and excluded names/counts are retained in evidence.
5. Derive all other drug attributes from the selected complete tuple. Uniform
   package choice within a selected group and seeded row shuffling are explicit
   sampler assumptions. Lookup multiplicity does not determine drug-group usage.

The final two steps cannot reproduce every separate name/class marginal. Independent
assessment records all reported-vs-actual deviations, tests exact effective group
counts, and applies a familywise 0.001 Hoeffding bound to within-group package
allocations. Unknown reported support is not interpreted as zero usage.

Temporal policy is identity-only: package marketing dates do not filter these
synthetic claims. Independent assessment compares dates and reports exceptions;
the original patient birth/coverage/death constraints still apply. No historical
availability or patient-specific clinical appropriateness is claimed. Current
NLM status identifies captured mappings only.

Run `verifier/check_pharm_enrichment.py` through the archived assessment runner,
verify RxNorm normalization against retained responses, audit bundled metadata,
and exercise actual PHARM ETL methods locally before selection. A passing scoped
assessment is not a promoted release or a successful full cloud ETL execution.
