import pytest

from precedent.config import ConfigError, razorpay_config


@pytest.fixture(autouse=True)
def clear_cache():
    razorpay_config.cache_clear()
    yield
    razorpay_config.cache_clear()


def test_raises_a_clear_error_when_credentials_are_missing(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)

    with pytest.raises(ConfigError, match="RAZORPAY_KEY_ID"):
        razorpay_config()


def test_loads_all_three_values_when_present(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret123")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsecret456")

    config = razorpay_config()

    assert config.key_id == "rzp_test_abc"
    assert config.key_secret == "secret123"
    assert config.webhook_secret == "whsecret456"
