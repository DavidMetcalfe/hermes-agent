"""Shape-tolerant pricing extraction from endpoint ``/models`` metadata.

Contract under test (PR 3): both observed pricing conventions normalize to
dollars-per-token, with the completion alias and ``global`` nesting handled.

- OpenRouter-style ($/token): ``{"prompt": "0.00000075", "completion": "0.00000375"}``
- Per-million style ($/M): ``{"global": {"prompt": 0.75, "completions": 3.75,
  "input_cache_read": 0.075}, "regional_increase_percent": 0.1}``
"""

from decimal import Decimal

from agent.pricing_shape import extract_pricing_fields, unwrap_pricing_container


class TestOpenRouterShape:
    def test_dollar_per_token_strings_unchanged(self):
        fields = extract_pricing_fields({"prompt": "0.00000075", "completion": "0.00000375"})
        assert fields["prompt"] == Decimal("0.00000075")
        assert fields["completion"] == Decimal("0.00000375")

    def test_expensive_model_token_price_below_ceiling_stays(self):
        """$8/M = 0.000008/token < 0.01 ceiling: still per-token."""
        fields = extract_pricing_fields({"prompt": "0.00001", "completion": "0.0003"})
        assert fields["prompt"] == Decimal("0.00001")
        assert fields["completion"] == Decimal("0.0003")


class TestPerMillionShape:
    def test_assemblyai_style_global_nesting(self):
        """The exact observed shape: $/M values, completions alias, global nest."""
        fields = extract_pricing_fields({
            "global": {"completions": 3.75, "prompt": 0.75, "input_cache_read": 0.075},
            "regional_increase_percent": 0.1,
        })
        assert fields["prompt"] == Decimal("0.75") / Decimal(1_000_000)
        assert fields["completion"] == Decimal("3.75") / Decimal(1_000_000)
        assert fields["cache_read"] == Decimal("0.075") / Decimal(1_000_000)

    def test_expensive_model_per_million(self):
        """$300/M output (frontier tier): >> 0.01, must normalize to 0.0003/token."""
        fields = extract_pricing_fields({"prompt": 30.0, "completions": 300.0})
        assert fields["prompt"] == Decimal("0.00003")
        assert fields["completion"] == Decimal("0.0003")

    def test_cache_write_alias(self):
        fields = extract_pricing_fields({"global": {"input_cache_write": 0.041667}})
        assert fields["cache_write"] == Decimal("0.041667") / Decimal(1_000_000)


class TestDegenerateInput:
    def test_missing_pricing_returns_all_none(self):
        fields = extract_pricing_fields({})
        assert all(v is None for v in fields.values())

    def test_non_dict_container(self):
        assert extract_pricing_fields(None) == {
            "prompt": None, "completion": None, "cache_read": None,
            "cache_write": None, "request": None,
        }

    def test_malformed_values_skipped(self):
        fields = extract_pricing_fields({"prompt": "not-a-number", "completion": True})
        assert fields["prompt"] is None
        assert fields["completion"] is None  # bool rejected

    def test_flat_container_unwrapped_passthrough(self):
        flat = {"prompt": "0.00000075"}
        assert unwrap_pricing_container(flat) == flat
