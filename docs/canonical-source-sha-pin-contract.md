# `canonical_source_sha` ingestion pin contract

For a Python package whose `canonical_source.type` is `"git"` ([LWLP-2111](https://redhat.atlassian.net/browse/LWLP-2111)),
`GitMirrorPublisher.publish_git_source` ([LWLP-3402](https://redhat.atlassian.net/browse/LWLP-3402))
resolves the configured `ref` to its exact upstream commit SHA at ingestion time
and writes it as a top-level `canonical_source_sha` string key in
`.lightwell/metadata.json`, committed at the same `<canonical_name>/<version>`
tag (and its `baseline/<version>` alias) that carries the mirrored source tree.
The pin is computed once, at first ingestion of a given package/version;
re-ingesting an already-tagged version never recomputes or overwrites it — a
moved upstream ref on a re-run fails closed instead. Balor-fianna's
`fetch_target_tree` stage ([balor-fianna!2098](https://gitlab.cee.redhat.com/lightwell/balor-fianna/-/merge_requests/2098))
reads this same key and compares it against the ref's live-resolved SHA at
build time, failing the build on a mismatch and passing permissively only when
no pin has been recorded yet (see [LWLP-2112](https://redhat.atlassian.net/browse/LWLP-2112)
for the sdist-only sync path this complements).
