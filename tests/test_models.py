"""Tests for taisce_cuan.models"""

import json

from taisce_cuan.models import (
    BuildDefinition,
    ExternalParameters,
    IngestionMetadata,
    Predicate,
    RunDetails,
    RunDetailsMetadata,
    Subject,
)


def _minimal_predicate() -> Predicate:
    return Predicate(
        buildDefinition=BuildDefinition(
            externalParameters=ExternalParameters(
                package="x", canonical_name="x", version="1.0",
            ),
        ),
        runDetails=RunDetails(
            metadata=RunDetailsMetadata(startedOn="t", finishedOn="t"),
        ),
    )


def test_canonical_source_sha_omitted_when_none():
    metadata = IngestionMetadata(
        subject=[Subject(name="x", digest={"sha256": "a" * 64})],
        predicate=_minimal_predicate(),
    )
    dumped = json.loads(metadata.model_dump_json(by_alias=True, exclude_none=True))
    assert "canonical_source_sha" not in dumped


def test_canonical_source_sha_included_when_set():
    metadata = IngestionMetadata(
        canonical_source_sha="a" * 40,
        subject=[Subject(name="x", digest={"sha256": "a" * 64})],
        predicate=_minimal_predicate(),
    )
    dumped = json.loads(metadata.model_dump_json(by_alias=True, exclude_none=True))
    assert dumped["canonical_source_sha"] == "a" * 40
