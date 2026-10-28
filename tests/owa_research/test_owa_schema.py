import pytest
from pydantic import ValidationError

from owa_research.core.schema import (
    Claim, ClaimKind, ResearchRequest, ResearchResult, ResearchStatus, Source, Usage,
)


def test_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ResearchRequest(task="x", commands=["rm -rf /"])


def test_preferred_and_blocked_are_exclusive():
    with pytest.raises(ValidationError, match="互斥"):
        ResearchRequest(task="x", preferred_domains=["a.com"], blocked_domains=["b.com"])
    ResearchRequest(task="x", preferred_domains=["a.com"])


def test_seed_urls_must_be_http():
    with pytest.raises(ValidationError):
        ResearchRequest(task="x", seed_urls=["file:///etc/passwd"])


def test_result_forbids_command_fields_and_unknown_source_ids():
    usage = Usage(wall_seconds=1.0)
    with pytest.raises(ValidationError):
        ResearchResult(status="complete", summary="s", usage=usage, next_actions=["do it"])
    with pytest.raises(ValidationError, match="source_id"):
        ResearchResult(
            status=ResearchStatus.COMPLETE, summary="s", usage=usage,
            claims=[Claim(text="t", kind=ClaimKind.SOURCE_STATEMENT, source_ids=["s9"])],
        )


def test_result_roundtrip():
    r = ResearchResult(
        status=ResearchStatus.PARTIAL, summary="s", usage=Usage(wall_seconds=2.5),
        sources=[Source(source_id="s1", url="https://example.com", fetched_at="2026-09-17T00:00:00Z")],
        claims=[Claim(text="t", kind=ClaimKind.INFERENCE, source_ids=["s1"])],
    )
    assert ResearchResult.model_validate_json(r.model_dump_json()) == r
    assert r.usage.tokens.cached_prompt is None
