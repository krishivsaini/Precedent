"""The response cache. The property under test is that it changes what a run *costs*,
never what it *measures*."""

import json

import pytest

from evals.cache import CachingLLM, cache_key
from precedent.adapters.llm.base import LLMUnavailable
from precedent.adapters.llm.scripted import ScriptedLLM


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


def caching(responses, cache_dir, **kwargs):
    return CachingLLM(ScriptedLLM(responses), cache_dir=cache_dir, **kwargs)


class TestCacheKey:
    def test_identical_requests_share_a_key(self):
        assert cache_key("m", "s", "u", 0.0) == cache_key("m", "s", "u", 0.0)

    @pytest.mark.parametrize("changed", [
        ("other-model", "s", "u", 0.0),
        ("m", "other-system", "u", 0.0),
        ("m", "s", "other-user", 0.0),
        ("m", "s", "u", 0.7),
    ])
    def test_anything_that_changes_the_answer_changes_the_key(self, changed):
        # This is what stops the cache laundering a stale result: a hit can only mean the
        # identical question was already put to the identical model.
        assert cache_key(*changed) != cache_key("m", "s", "u", 0.0)

    def test_field_boundaries_cannot_be_forged_by_concatenation(self):
        # Without a separator between fields, ("ab", "c") and ("a", "bc") would collide.
        assert cache_key("m", "ab", "c", 0.0) != cache_key("m", "a", "bc", 0.0)


class TestReplay:
    def test_a_repeated_request_is_served_without_calling_the_model(self, cache_dir):
        llm = caching(["first"], cache_dir)
        assert llm.complete("s", "u").text == "first"
        # The scripted client has nothing queued, so a second call reaching it would raise.
        assert llm.complete("s", "u").text == "first"
        assert llm.stats() == {"hits": 1, "misses": 1}

    def test_a_different_request_still_reaches_the_model(self, cache_dir):
        llm = caching(["first", "second"], cache_dir)
        assert llm.complete("s", "u1").text == "first"
        assert llm.complete("s", "u2").text == "second"
        assert llm.stats() == {"hits": 0, "misses": 2}

    def test_a_replayed_response_is_marked_cached(self, cache_dir):
        # So latency statistics can exclude it: a cache hit's microseconds measure the disk.
        llm = caching(["first"], cache_dir)
        assert llm.complete("s", "u").cached is False
        assert llm.complete("s", "u").cached is True

    def test_replay_preserves_token_counts(self, cache_dir):
        llm = caching(["first"], cache_dir)
        fresh = llm.complete("s", "u")
        replayed = llm.complete("s", "u")
        assert replayed.prompt_tokens == fresh.prompt_tokens
        assert replayed.model == fresh.model

    def test_a_new_process_reads_what_an_earlier_one_wrote(self, cache_dir):
        # The whole point: a run killed by a quota exhaustion resumes tomorrow.
        caching(["first"], cache_dir).complete("s", "u")
        resumed = caching([], cache_dir)
        assert resumed.complete("s", "u").text == "first"
        assert resumed.stats()["hits"] == 1


class TestFailureHandling:
    def test_failures_are_never_cached(self, cache_dir):
        # Caching an outage would make it permanent — every later run would replay the
        # failure instead of retrying, and the eval could never recover.
        llm = caching([LLMUnavailable("down"), "recovered"], cache_dir)
        with pytest.raises(LLMUnavailable):
            llm.complete("s", "u")
        assert llm.complete("s", "u").text == "recovered"

    def test_a_corrupt_entry_is_discarded_rather_than_crashing_the_run(self, cache_dir):
        llm = caching(["fresh"], cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = cache_key(llm.model, "s", "u", 0.0)
        (cache_dir / f"{key}.json").write_text("{not json", encoding="utf-8")
        assert llm.complete("s", "u").text == "fresh"

    def test_an_entry_from_an_older_schema_is_discarded(self, cache_dir):
        llm = caching(["fresh"], cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = cache_key(llm.model, "s", "u", 0.0)
        (cache_dir / f"{key}.json").write_text(json.dumps({"old": "shape"}), encoding="utf-8")
        assert llm.complete("s", "u").text == "fresh"

    def test_no_temporary_files_are_left_behind(self, cache_dir):
        llm = caching(["first"], cache_dir)
        llm.complete("s", "u")
        assert list(cache_dir.glob("*.tmp")) == []


class TestDisabled:
    def test_disabling_bypasses_the_cache_entirely(self, cache_dir):
        llm = caching(["first", "second"], cache_dir, enabled=False)
        assert llm.complete("s", "u").text == "first"
        assert llm.complete("s", "u").text == "second"
        assert not cache_dir.exists()

    def test_it_is_a_transparent_stand_in_for_the_client(self, cache_dir):
        llm = caching([], cache_dir)
        assert llm.model == "scripted-test-double"
