"""Groq and NVIDIA NIM are configuration over `OpenAICompatibleClient`.

Their request handling is tested once, in `test_openai_compatible.py`. What is left to
verify is that each is pointed at the right endpoint, reads its own key through
`precedent.config` (so `.env` is honoured — an adapter reading `os.environ` directly works
under pytest and finds nothing from a bare CLI), and carries the rate limit that provider
actually imposes.
"""

from precedent.adapters.llm.groq import BASE_URL as GROQ_URL
from precedent.adapters.llm.groq import DEFAULT_TOKENS_PER_MINUTE, GroqClient
from precedent.adapters.llm.nvidia import BASE_URL as NVIDIA_URL
from precedent.adapters.llm.nvidia import NvidiaClient
from precedent.adapters.llm.openai_compatible import OpenAICompatibleClient


class TestGroq:
    def test_is_an_openai_compatible_client(self):
        assert isinstance(GroqClient(api_key="k"), OpenAICompatibleClient)

    def test_posts_to_the_groq_chat_completions_endpoint(self):
        assert GroqClient(api_key="k")._url == f"{GROQ_URL}/chat/completions"

    def test_reads_its_key_through_config(self, monkeypatch):
        monkeypatch.setattr("precedent.adapters.llm.groq.groq_api_key", lambda: "from-dotenv")
        assert GroqClient()._api_key == "from-dotenv"

    def test_an_explicit_key_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setattr("precedent.adapters.llm.groq.groq_api_key", lambda: "from-dotenv")
        assert GroqClient(api_key="explicit")._api_key == "explicit"

    def test_paces_against_the_measured_token_limit(self):
        # 8000 TPM, read from `x-ratelimit-limit-tokens` on a live response rather than
        # guessed. Without the throttle the grounded arm fails on 429s.
        assert DEFAULT_TOKENS_PER_MINUTE == 8000
        assert GroqClient(api_key="k")._throttle is not None

    def test_names_itself_in_error_messages(self):
        assert GroqClient(api_key="k").provider == "Groq"


class TestNvidia:
    def test_is_an_openai_compatible_client(self):
        assert isinstance(NvidiaClient(api_key="k"), OpenAICompatibleClient)

    def test_posts_to_the_nim_chat_completions_endpoint(self):
        assert NvidiaClient(api_key="k")._url == f"{NVIDIA_URL}/chat/completions"

    def test_reads_its_key_through_config(self, monkeypatch):
        monkeypatch.setattr(
            "precedent.adapters.llm.nvidia.nvidia_api_key", lambda: "from-dotenv"
        )
        assert NvidiaClient()._api_key == "from-dotenv"

    def test_has_no_throttle_by_default(self):
        # NIM does not publish a per-minute token limit the way Groq does in its headers.
        # Guessing one and pacing against it would slow the run for no reason; the retry
        # path still handles throttling if it appears.
        assert NvidiaClient(api_key="k")._throttle is None

    def test_a_throttle_can_still_be_imposed(self):
        assert NvidiaClient(api_key="k", tokens_per_minute=5000)._throttle is not None

    def test_names_itself_in_error_messages(self):
        assert NvidiaClient(api_key="k").provider == "NVIDIA NIM"
