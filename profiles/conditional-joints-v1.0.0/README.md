# Conditional joint profile v1.0.0

This profile selects the reusable `conditional_joint_table` type for a CTC-only
replacement. The recipe pins this file, shared Rule 4, the count snapshot and the
promoted r3 baseline. A recipe is a set of explicit input/version selections;
the generic engine interprets the selected blocks.

## Selected target

For each patient's index tumor, draw `(sex, race, extended ICCC)` jointly given
its retained age. Broadcast sex and race to that patient's other tumor rows.
For each later tumor, draw extended ICCC given its retained age and the patient's
drawn sex/race. Only an absent later-tumor stratum uses the declared sex/race
fallback. Both draws use the through-2022 **all-tumor** count table. ICCC parents
come from the shared hierarchy, never independent draws.

The p16 block draws `(schemaId, p16Recode)` jointly given the retained diagnosis
year, using independently validated applicability support. It does not establish
schema consistency with ICCC, site or histology.

This preserves r3 ages, timeline/context fields, patient/tumor keys and all other
source files. Consequently it targets conditional distributions rather than the
unrestricted observed G1 joint distribution. Source clinical validity and new
associations with unchanged treatment/claims data remain unverified.

## Adding another categorical group

1. Freeze a new aggregate snapshot with code-string columns and positive integer
   `count` values; retain missing-value codes explicitly and pin the manifest.
2. Add a shared conditional-joint requirement with its fields, distributions,
   selectors, broadcast policy and table paths in a new rule version.
3. Add profile blocks specifying `given`, `outputs`, table pins and explicit
   `backoff` levels. Condition on relevant earlier groups to preserve their links.
4. Bind every required distribution to exactly one block, then pin the new profile
   and rule version in a new recipe. Keep prior frozen versions unchanged.
5. Generate into a fresh development directory and inspect the complete
   independent audit, including fallback counts and conditional discrepancies.

The engine already supports new categorical fields and separate snapshots without
source edits. Blocks must follow the producers of their conditioning fields;
fallback must retain those generated conditioning fields. Duplicate writers on
overlapping rows, unsupported required capabilities and missing support fail.
Multi-rule dependencies within a block are validated by the generic sampler.
The CTC replacement adapter refuses writes to context-bound fields; a group that
changes age, timing or outcomes needs coordinated generation of dependent sources.

## Execution and evidence

From the workspace root:

```sh
PYTHONDONTWRITEBYTECODE=1 experiments/gen-sdv/.venv/bin/python -m jamboree_sdv.cli replace-ctc \
  --workspace "$PWD" \
  --recipe experiments/gen-sdv/recipes/ctc-rules-v4-10pct-r4.json \
  --out experiments/gen-sdv/work/runs/CHOOSE-A-NEW-RUN-ID
```

The command writes `CTC.zip`, archived inputs/code, `generation.json`,
`candidate.json` and an independent `audit/`. Check both its process exit and
`audit/completion.json`. No passing candidate automatically promotes, uploads,
or replaces the existing release. Statistical checks use a predeclared family
alpha of 0.001 and report sparse-support limitations; they do not certify clinical
validity or prove equality of every observed and generated distribution.
