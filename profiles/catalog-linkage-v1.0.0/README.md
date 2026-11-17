# Rule-driven CCDI/CPI catalog profile v1.0.0

The shared rules own catalog membership and mapping consistency. This profile
owns the gen-sdv sampling choice: condition the published `resourceName` counts
on positive-count names present in the pinned catalog. It preserves the existing
`CCDI.resourceName.catalog_v1` random stream. Counts in the mock catalog are never
weights, and public display labels never replace literal resource names.

`jamboree_sdv.catalog_linkage.regenerate_from_rules(rows, rules_pin, profile_pin,
seed)` accepts mapping triples `(nccrId, resourceParticipantId, resourceName)`
and returns regenerated triples plus an explicit sampling report. File pins
contain exactly `path` and `sha256`. The supported immutable rules v2.3.0 hash is
`1ef2a47229785b209d5e1514d548739816879233a57728811e81211e2e64c3ed`;
the profile hash is
`e99ecfb1e9ceb13fae3370740c2a66f5d69205ea7df92956a57ab0a8cddbfec0`.
Changed bundles or profiles require an explicit capability update; re-pinning
unsupported semantics does not make them supported.

The scoped sampler preserves patient/participant IDs and requires its existing
globally unique synthetic participant IDs. That stronger convention belongs to
this sampler: the shared rule permits the same participant ID in different
resource namespaces. New samplers can implement that broader rule directly.

Write one raw CCDI row per patient with numbered mapping pairs and CPI as the
same distinct triples. Then validate both final payloads against CTC. The checker
recomputes projection, identity and frequency results independently of sampling;
only its strict declarative input reader is shared with the adapter. It uses a
simultaneous Hoeffding bound with family alpha 0.001. Passing does not establish
clinical joint fidelity or evaluate the inherited non-catalog rules.

From the workspace root, the checker command accepts a delivery manifest with
`files` entries containing `source`, `local_path`, `sha256` and `size_bytes`:

```text
experiments/gen-sdv/.venv/bin/python experiments/gen-sdv/verifier/check_catalog_rules.py \
  --manifest <release.json> --manifest-sha256 <sha256> \
  --rules reference/generation-rules/v2.3.0/rules.json --rules-sha256 <sha256> \
  --profile experiments/gen-sdv/profiles/catalog-linkage-v1.0.0/profile.json --profile-sha256 <sha256> \
  --out experiments/gen-sdv/work/validation/<dataset>/rules-v2.3.0/<new-timestamp>
```

Set `PYTHONDONTWRITEBYTECODE=1`. Existing output destinations are refused. The
checker verifies the selected release and CTC/CCDI/CPI hashes, archives small
inputs and checker source, and records other sources/rules as unevaluated.
Archived input paths retain their original meaning; this scoped evidence is not
a relocated or newly qualified full release. Missing or invalid required inputs
stop verification with an error, never a passing assessment.

Current r2 passed all three new rules: 165 raw patient rows, 697 mappings, 31
observed catalog names. Of 69,705 reported public mappings, 49,965 remain after
conditioning on the catalog (34 supported names); 29 additional mappings in the
69,734 public total have unreported names. Both the sampler and checker report
these quantities and excluded names/counts.

Ordinary all-source generation does not yet select this profile. Its loader
continues to reject unsupported v2.3.0 rules. Full pipeline integration and 10%
scale support remain separate work; this change does not regenerate or deploy r3.
