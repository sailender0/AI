"""Pins _estimate_cost — token-count → USD, driven by the configurable
per-1M prices (config.py). Prices must track AZURE_OPENAI_DEPLOYMENT."""
import pytest

from app.ai.llm import _estimate_cost
from app.config import settings


def test_input_price_is_per_million_tokens():
    assert _estimate_cost(1_000_000, 0) == pytest.approx(settings.AZURE_OPENAI_PRICE_IN)


def test_output_price_is_per_million_tokens():
    assert _estimate_cost(0, 1_000_000) == pytest.approx(settings.AZURE_OPENAI_PRICE_OUT)


def test_zero_tokens_is_zero_cost():
    assert _estimate_cost(0, 0) == 0.0


def test_combined_is_sum_of_both_rates():
    assert _estimate_cost(1_000_000, 1_000_000) == pytest.approx(
        settings.AZURE_OPENAI_PRICE_IN + settings.AZURE_OPENAI_PRICE_OUT
    )


if __name__ == "__main__":
    for _n, _f in list(globals().items()):
        if _n.startswith("test_"):
            _f()
    print("ok")
