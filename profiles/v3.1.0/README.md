# All-source model v3.1.0

Successor to the frozen v3.0.0 model. Field policies remain v3.0.0. The model
requires the separate pinned tumor-multiplicity v1.0.0 profile and supports
multiple ordered tumor dates per patient. Other all-source settings are retained.

Selected by `recipes/all-sources-integrated-v3.json`, the default for new CLI
`run` requests. Shared domain rules remain v2.3.0; generator model versions and
shared rule versions are separate. Rule 4's CTC-only replacement continues to
preserve its selected baseline population and does not repair older datasets.
