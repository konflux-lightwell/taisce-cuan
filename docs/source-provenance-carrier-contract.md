# Source provenance carrier contract

This contract defines the file closure that Taisce Cuan produces, Fromager
carries through normalization, and Taisce Cuan validates before publishing a
Python source mirror.

## Purpose and ownership

`fetch-source` acquires the original upstream source distribution and records
its origin evidence. It does **not** normalize the archive.

`python-fromager-build-sdist` normalizes the acquired archive. It preserves the
complete acquisition carrier byte-for-byte and adds the normalized archive plus
the transformation record. It does not interpret upstream provenance.

`archive-sources` consumes the normalized carrier. It validates the complete
closure, creates the final Lightwell metadata/attestations, and only then
commits, tags, and pushes the Git mirror.

## Acquired carrier

The existing `fetch-source` Trusted Artifact has this relative layout:

```text
downloads/
  <canonical-package>-<version>.tar.gz  # exact upstream-acquired sdist
source-origin.json                      # Taisce acquisition record
rhtl-index.pep691.json                  # RHTL only
provenance.pep740.json                  # RHTL only; advertised provenance only
```

`source-origin.json` binds the selected upstream artifact URL, declared and
verified SHA-256, and the relative `downloads/` path. For RHTL it also records
one of these evidence states:

- **advertised**: `provenance.pep740.json` is the exact response retrieved from
the provenance URL advertised by the selected PEP 691 entry;
- **not advertised**: `rhtl-index.pep691.json` is the exact PEP 691 response
showing that the selected entry did not advertise provenance;
- **PyPI**: no RHTL evidence file is present.

The RHTL index and advertised provenance response are preserved as exact raw
bytes. `provenance.pep740.json` is opaque upstream evidence: it is not
converted to Cosign DSSE, parsed/re-serialized for storage, or re-signed by
Lightwell.

## Normalized carrier

The existing Fromager task preserves the complete acquired carrier and adds:

```text
<normalized-sdist>.tar.gz               # Fromager output at carrier root
sdist-transformation.json               # acquired input -> normalized output
```

`sdist-transformation.json` binds:

- SHA-256 of the exact original acquired sdist;
- relative name and SHA-256 of the normalized sdist;
- SHA-256 of the exact `source-origin.json` bytes.

The acquired and normalized sdist hashes are expected to differ when
normalization changes the archive. They must never be compared as though they
are the same artifact.

## Final Git mirror

`archive-sources` validates the normalized carrier, unpacks the normalized
sdist into `source/`, and writes:

```text
.lightwell/metadata.json
.lightwell/metadata.dsse.json
.lightwell/source-origin.json
.lightwell/sdist-transformation.json
```

For PyPI only it also writes a Lightwell-generated:

```text
.lightwell/provenance.dsse.json
```

For RHTL, the final mirror instead retains exactly the applicable opaque
upstream evidence:

```text
.lightwell/provenance.pep740.json       # advertised provenance
# or
.lightwell/rhtl-index.pep691.json       # provenance not advertised
```

`metadata.dsse.json` is Lightwell-signed for every published route. There is
no signed final Git tree hash: adding metadata and attestation files changes
the Git tree, so the signed binding is the normalized archive and evidence
closure instead.

## Fail-closed requirements

In this contract, **fail closed** means the stage exits unsuccessfully and does
not produce its next artifact or publish the Git mirror when a required fact
cannot be established. It does not mean substituting a weaker provenance state
or continuing with unsigned metadata.

### Acquisition fails when

- no exact source artifact can be selected or its declared SHA-256 is missing;
- the downloaded original sdist does not match the selected entry's SHA-256;
- an advertised RHTL provenance value is empty, malformed, non-HTTPS, or cannot
  be retrieved successfully;
- a retrieved RHTL provenance response cannot be preserved and digest-bound.

A genuinely omitted/null RHTL provenance field is the explicit `not advertised`
state. It is not an error, but its PEP 691 index evidence is required.

### Normalization fails when

- the required acquired sdist is absent from the acquisition carrier; or
- Fromager cannot produce the normalized sdist or transformation record.

Normalization does not decide whether RHTL evidence is valid; it carries the
acquisition closure unchanged.

### Final archive/publish fails when

- `source-origin.json`, original acquired archive, normalized sdist, or
  `sdist-transformation.json` is missing or its recorded digest/path does not
  match actual bytes;
- the applicable RHTL evidence file is missing, altered, or inconsistent with
  the source-origin evidence state;
- required Lightwell metadata signing or verification fails;
- any signed metadata/evidence file changes after signing and before publish.

Git commit, tag, and atomic push occur only after all final validation and
Lightwell signature verification succeed. There is no unsigned publication
fallback.
