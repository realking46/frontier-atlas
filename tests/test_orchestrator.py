from __future__ import annotations

import pytest

from frontier_atlas.core.errors import (
    AllProvidersFailedError,
    FatalProviderError,
    NoEntityFoundError,
    PayloadTooLargeError,
    RateLimitError,
    TransientError,
)
from frontier_atlas.extraction.orchestrator import LLMOrchestrator, parse_json_object
from frontier_atlas.schemas import StartupExtraction

from .conftest import PAGE, FakeProvider, as_json

GOOD = as_json(entityName="Acme Robotics", employeeCount=120, foundedYear=2019,
               location="Berlin, Germany", website="https://acmerobotics.ai")


async def test_happy_path(settings, sleeps):
    a = FakeProvider("a", [GOOD])
    orch = LLMOrchestrator([a], settings, sleep=sleeps)
    res = await orch.extract(PAGE, StartupExtraction, source_url="https://example.com/acme")
    assert res.provider == "a"
    assert res.data.entityName == "Acme Robotics"
    assert res.data.employeeCount == 120
    assert res.dropped_fields == []
    assert sleeps.calls == []


async def test_429_backs_off_then_falls_through(settings, sleeps):
    a = FakeProvider("a", [RateLimitError("429", retry_after=0.02)] * 3)
    b = FakeProvider("b", [GOOD])
    orch = LLMOrchestrator([a, b], settings, sleep=sleeps)
    res = await orch.extract(PAGE, StartupExtraction)
    assert res.provider == "b"
    assert len(sleeps.calls) == 2                  # 2 retries on a, then fall through
    assert all(s >= 0.02 for s in sleeps.calls)    # Retry-After honoured
    assert [x.outcome for x in res.attempts] == ["429", "429", "429", "ok"]

    # a is now cooling down: the next extraction goes straight to b
    b.script.append(GOOD)
    res2 = await orch.extract(PAGE, StartupExtraction)
    assert res2.attempts[0].outcome == "cooldown"
    assert len(a.prompts) == 3


async def test_429_with_quota_sized_retry_after_falls_through_without_sleeping(settings, sleeps):
    # live run: Gemini's free-tier daily cap answered retryDelay=33140s (9 h)
    a = FakeProvider("a", [RateLimitError("429", retry_after=33140.0)])
    b = FakeProvider("b", [GOOD, GOOD])
    orch = LLMOrchestrator([a, b], settings, sleep=sleeps)
    res = await orch.extract(PAGE, StartupExtraction)
    assert res.provider == "b" and sleeps.calls == []          # no 9-hour sleep
    assert [x.outcome for x in res.attempts] == ["quota", "ok"]
    res2 = await orch.extract(PAGE, StartupExtraction)        # a stays cooled down for the quota window
    assert res2.attempts[0].outcome == "cooldown" and int(res2.attempts[0].detail[:-6]) > 30000
    assert len(a.prompts) == 1


async def test_429_recovers_on_same_provider(settings, sleeps):
    a = FakeProvider("a", [RateLimitError("429"), GOOD])
    orch = LLMOrchestrator([a], settings, sleep=sleeps)
    res = await orch.extract(PAGE, StartupExtraction)
    assert res.provider == "a"
    assert len(sleeps.calls) == 1


async def test_413_shrinks_payload_and_retries(settings, sleeps):
    long_page = PAGE + "\n" + "\n\n".join(f"Filler paragraph number {i} about nothing in particular." for i in range(600))
    a = FakeProvider("a", [PayloadTooLargeError("413"), PayloadTooLargeError("413"), GOOD], max_input_tokens=6000)
    orch = LLMOrchestrator([a], settings, sleep=sleeps)
    res = await orch.extract(long_page, StartupExtraction)
    sizes = [len(p) for p in a.prompts]
    assert sizes[0] > sizes[1] > sizes[2]          # each 413 makes the payload smaller
    assert res.chunk.truncated
    assert "120 employees" in a.prompts[-1]        # dense facts survive shrinking
    assert sleeps.calls == []                      # 413 needs no waiting


async def test_413_exhausted_falls_through(settings, sleeps):
    a = FakeProvider("a", [PayloadTooLargeError("413")] * 4)
    b = FakeProvider("b", [GOOD])
    res = await LLMOrchestrator([a, b], settings, sleep=sleeps).extract(PAGE, StartupExtraction)
    assert res.provider == "b"


async def test_transient_retry(settings, sleeps):
    a = FakeProvider("a", [TransientError("503"), TransientError("timeout"), GOOD])
    res = await LLMOrchestrator([a], settings, sleep=sleeps).extract(PAGE, StartupExtraction)
    assert res.provider == "a" and len(sleeps.calls) == 2


async def test_invalid_json_gets_one_repair(settings, sleeps):
    a = FakeProvider("a", ["Sure! Here is the data you asked for", "```json\n" + GOOD + "\n```"])
    res = await LLMOrchestrator([a], settings, sleep=sleeps).extract(PAGE, StartupExtraction)
    assert res.data.entityName == "Acme Robotics"
    assert "rejected" in a.prompts[1]


async def test_hallucinated_identity_is_rejected_then_falls_through(settings, sleeps):
    fake = as_json(entityName="Globex Corporation", employeeCount=5000)
    a = FakeProvider("a", [fake, fake])
    b = FakeProvider("b", [GOOD])
    res = await LLMOrchestrator([a, b], settings, sleep=sleeps).extract(PAGE, StartupExtraction)
    assert res.provider == "b"
    assert res.data.entityName == "Acme Robotics"


async def test_hallucinated_secondary_field_is_nulled(settings, sleeps):
    a = FakeProvider("a", [as_json(entityName="Acme Robotics", employeeCount=999, location="Paris")])
    res = await LLMOrchestrator([a], settings, sleep=sleeps).extract(PAGE, StartupExtraction)
    assert res.data.employeeCount is None
    assert res.data.location is None
    assert set(res.dropped_fields) == {"employeeCount", "location"}


async def test_no_entity_stops_chain(settings, sleeps):
    a = FakeProvider("a", [as_json(entityName=None)])
    b = FakeProvider("b", [GOOD])
    with pytest.raises(NoEntityFoundError):
        await LLMOrchestrator([a, b], settings, sleep=sleeps).extract("Cookie policy page.", StartupExtraction)
    assert b.prompts == []


async def test_fatal_disables_provider(settings, sleeps):
    a = FakeProvider("a", [FatalProviderError("401 bad key")])
    b = FakeProvider("b", [GOOD, GOOD])
    orch = LLMOrchestrator([a, b], settings, sleep=sleeps)
    await orch.extract(PAGE, StartupExtraction)
    await orch.extract(PAGE, StartupExtraction)
    assert len(a.prompts) == 1


async def test_all_fail(settings, sleeps):
    a = FakeProvider("a", [FatalProviderError("401")])
    b = FakeProvider("b", [TransientError("500")] * 3)
    with pytest.raises(AllProvidersFailedError) as ei:
        await LLMOrchestrator([a, b], settings, sleep=sleeps).extract(PAGE, StartupExtraction)
    assert [p for p, _ in ei.value.errors] == ["a", "b"]


def test_parse_json_object_variants():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('blah {"a": 1} blah') == {"a": 1}
    assert parse_json_object('[{"a": 1}]') == {"a": 1}
    assert parse_json_object("nope") is None


def test_grounding_accepts_the_pages_own_site_but_not_other_urls():
    from frontier_atlas.extraction import grounding
    from frontier_atlas.schemas import StartupExtraction

    text = "Chroma. Open-source search infrastructure for AI. Get started."
    d = StartupExtraction(entityName="Chroma", website="https://trychroma.com")
    assert grounding.check(d, text, kind="startup", source_url="https://www.trychroma.com/").dropped == []
    d = StartupExtraction(entityName="Chroma", website="https://chroma-db.example")      # invented
    assert grounding.check(d, text, kind="startup", source_url="https://www.trychroma.com/").dropped == ["website"]
    d = StartupExtraction(entityName="Chroma", website="https://trychroma.com")
    assert grounding.check(d, text, kind="startup").dropped == ["website"]                 # no URL given: strict
