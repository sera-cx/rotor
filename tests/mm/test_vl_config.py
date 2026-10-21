"""Tests for TOML parsing and validation of VL market-maker config."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

# vl_config's TOML parser: tomllib on Python 3.11+, tomli on 3.10.
from rotor.mm.vl_config import parse_vl_config, tomllib


def _config(**overrides):
    raw = {
        "budget": [{"token": "USDC", "amount": "5000"}],
        "fixed_bps": "8",
        "return_bps": "1",
        "pair": [
            {"market": "XSGD/USDC", "price_source": "wise", "qty_base": "1000"},
            {
                "market": "EURC/USDC",
                "price_source": "ecb",
                "qty_base": "500",
                "fixed_bps": "6",
            },
        ],
    }
    raw.update(overrides)
    return raw


def test_parse_vl_config_accepts_multi_budget_toml_shape():
    cfg = parse_vl_config(_config(budget=[
        {"token": "USDC", "amount": "5000"},
        {"token": "XSGD", "amount": "1000"},
    ]))

    assert cfg.return_bps == Decimal("1")
    assert cfg.budget_tokens == {"USDC", "XSGD"}
    assert cfg.pairs[0].market == "XSGD/USDC"
    assert cfg.pairs[1].fixed_bps == Decimal("6")


def test_parse_vl_config_rejects_duplicate_or_inverse_markets():
    with pytest.raises(ValueError, match="duplicate or inverse"):
        parse_vl_config(_config(pair=[
            {"market": "XSGD/USDC", "qty_base": "100"},
            {"market": "USDC/XSGD", "qty_base": "100"},
        ]))


def test_parse_vl_config_defaults_return_bps_to_two():
    raw = _config()
    del raw["return_bps"]

    assert parse_vl_config(raw).return_bps == Decimal("2")


def test_parse_vl_config_rejects_non_positive_return_bps():
    with pytest.raises(ValueError, match="return_bps must be > 0"):
        parse_vl_config(_config(return_bps="0"))


def test_parse_vl_config_rejects_missing_budget():
    with pytest.raises(ValueError, match="budget"):
        parse_vl_config(_config(budget=[]))


def test_parse_vl_config_rejects_unsupported_price_source():
    with pytest.raises(ValueError, match="unsupported price_source 'manual'"):
        parse_vl_config(_config(pair=[
            {"market": "XSGD/USDC", "price_source": "manual", "qty_base": "100"},
        ]))


def test_parse_vl_config_rejects_non_positive_budget_amount():
    with pytest.raises(ValueError, match="budget USDC amount must be > 0"):
        parse_vl_config(_config(budget=[{"token": "USDC", "amount": "0"}]))


def test_parse_vl_config_rejects_duplicate_budget_token():
    with pytest.raises(ValueError, match="duplicate budget token USDC"):
        parse_vl_config(_config(budget=[
            {"token": "USDC", "amount": "1"},
            {"token": "usdc", "amount": "2"},
        ]))


def test_parse_vl_config_rejects_missing_pair():
    with pytest.raises(ValueError, match="at least one \\[\\[pair\\]\\]"):
        parse_vl_config(_config(pair=[]))


def test_parse_vl_config_rejects_non_positive_pair_quantity():
    with pytest.raises(ValueError, match="qty_base must be > 0"):
        parse_vl_config(_config(pair=[
            {"market": "XSGD/USDC", "price_source": "wise", "qty_base": "0"},
        ]))


def test_parse_vl_config_rejects_invalid_decimal():
    with pytest.raises(ValueError, match="fixed_bps must be decimal"):
        parse_vl_config(_config(fixed_bps="not-decimal"))


def test_parse_vl_config_normalizes_market_names():
    cfg = parse_vl_config(_config(pair=[
        {"market": "xsgd_usdc", "price_source": "wise", "qty_base": "100"},
    ]))

    assert cfg.pairs[0].market == "XSGD/USDC"


def test_parse_vl_config_pair_overrides_inherit_global_defaults():
    cfg = parse_vl_config(_config(
        fixed_bps="9",
        pair=[
            {"market": "XSGD/USDC", "price_source": "wise", "qty_base": "100"},
        ],
    ))

    pair = cfg.pairs[0]
    assert pair.fixed_bps == Decimal("9")


def test_a_global_setting_written_inside_a_budget_table_is_rejected():
    # In TOML, fixed_bps after the [[budget]] header belongs to that budget.
    with pytest.raises(ValueError, match="unknown key.*fixed_bps.*must come before"):
        parse_vl_config(_config(budget=[{"token": "USDC", "amount": "5000", "fixed_bps": "50"}]))


@pytest.mark.parametrize("doc", ["README.md", "docs/vl-mm.md"])
def test_the_documented_example_config_sets_what_it_shows(doc):
    text = (Path(__file__).resolve().parents[2] / doc).read_text()
    example = re.search(r"```toml\n(.*?)```", text, re.S).group(1)
    cfg = parse_vl_config(tomllib.loads(example))

    assert cfg.fixed_bps == Decimal("50")
    assert cfg.return_bps == Decimal("2")
    assert [pair.fixed_bps for pair in cfg.pairs] == [Decimal("50"), Decimal("40")]
