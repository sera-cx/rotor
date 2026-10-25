"""Source builders shared by the price_reference tests (imported by bare name)."""

from __future__ import annotations

import httpx

from rotor.price_reference.sources import EcbRateSource, WiseRateSource


def wise_source(handler) -> WiseRateSource:
    return WiseRateSource(
        url="https://api.wise.test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def ecb_source(date: str) -> EcbRateSource:
    """ECB's namespaced daily feed for `date`: 1 EUR = 1.20 USD = 1.50 SGD."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
      xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
      <Cube>
        <Cube time="{date}">
          <Cube currency="USD" rate="1.20"/>
          <Cube currency="SGD" rate="1.50"/>
        </Cube>
      </Cube>
    </gesmes:Envelope>
    """
    return EcbRateSource(http=httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, text=xml))))
