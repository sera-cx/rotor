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


def test_parse_vl_config_defaults_return_bps_to_two():
    raw = _config()
    del raw["return_bps"]

    assert parse_vl_config(raw).return_bps == Decimal("2")


def test_parse_vl_config_normalizes_market_names():
    cfg = parse_vl_config(_config(pair=[
        {"market": "xsgd_usdc", "price_source": "wise", "qty_base": "100"},
    ]))

    assert cfg.pairs[0].market == "XSGD/USDC"


def _pair(**fields):
    return {"market": "XSGD/USDC", "price_source": "wise", "qty_base": "100", **fields}


@pytest.mark.parametrize(("overrides", "match"), [
    pytest.param({"pair": [_pair(), _pair(market="USDC/XSGD")]}, "duplicate or inverse",
                 id="inverse-market"),
    pytest.param({"return_bps": "0"}, "return_bps must be more than 0", id="return-bps-zero"),
    pytest.param({"budget": []}, "budget", id="no-budget"),
    pytest.param({"pair": [_pair(price_source="manual")]}, "unsupported price_source 'manual'",
                 id="unknown-source"),
    pytest.param({"pair": [_pair(price_source="mas")]}, "'mas' was removed .* use 'ecb' or 'bnm'",
                 id="mas-removed"),
    pytest.param({"budget": [{"token": "USDC", "amount": "0"}]}, "budget USDC amount must be > 0",
                 id="budget-zero"),
    pytest.param({"budget": [{"token": "USDC", "amount": "1"}, {"token": "usdc", "amount": "2"}]},
                 "duplicate budget token USDC", id="duplicate-budget"),
    pytest.param({"pair": []}, "at least one \\[\\[pair\\]\\]", id="no-pair"),
    pytest.param({"pair": [_pair(qty_base="0")]}, "qty_base must be > 0", id="qty-zero"),
    pytest.param({"fixed_bps": "not-decimal"}, "fixed_bps must be decimal", id="bad-decimal"),
    # In TOML, fixed_bps after the [[budget]] header belongs to that budget.
    pytest.param({"budget": [{"token": "USDC", "amount": "5000", "fixed_bps": "50"}]},
                 "unknown key.*fixed_bps.*must come before", id="global-in-budget-table"),
    pytest.param({"pair": [_pair(return_bps="5")]}, "unknown key.*return_bps.*must come before",
                 id="global-in-pair-table"),
    pytest.param({"fallback_bp": "50"}, "unknown setting.*fallback_bp", id="misspelled-global"),
    pytest.param({"fallback_bps": "300"}, "fallback_weekend_bps .* must be at least fallback_bps",
                 id="weekend-spread-below-weekday"),
    pytest.param({"budget": [{"token": "USDCC", "amount": "5000"}]},
                 "budget token USDCC is not traded by any configured market", id="untraded-budget"),
])
def test_an_invalid_config_is_rejected(overrides, match):
    with pytest.raises(ValueError, match=match):
        parse_vl_config(_config(**overrides))


@pytest.mark.parametrize("doc", ["README.md", "docs/vl-mm.md"])
def test_the_documented_example_config_sets_what_it_shows(doc):
    text = (Path(__file__).resolve().parents[2] / doc).read_text()
    example = re.search(r"```toml\n(.*?)```", text, re.S).group(1)
    cfg = parse_vl_config(tomllib.loads(example))

    assert cfg.fixed_bps == Decimal("50")
    assert cfg.return_bps == Decimal("2")
    assert [pair.fixed_bps for pair in cfg.pairs] == [Decimal("50"), Decimal("40")]


@pytest.mark.parametrize(("where", "value"), [
    ("global", "0"), ("global", "-10"), ("global", "10000"), ("global", "NaN"),
    ("pair", "Infinity"), ("pair", "12000"),
])
def test_spreads_must_be_finite_and_between_0_and_10000_bps(where, value):
    raw = _config()
    if where == "global":
        raw["fixed_bps"] = value
    else:
        raw["pair"][0]["fixed_bps"] = value

    with pytest.raises(ValueError, match="fixed_bps must be"):
        parse_vl_config(raw)
