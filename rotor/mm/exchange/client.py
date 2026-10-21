"""Small Sera REST client used by Rotor VL market making."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from rotor.mm.exchange import eip712, errors
from rotor.mm.exchange.known_tokens import KNOWN_TOKENS

# Hosts for which an http:// base URL is tolerated (local dev/mocks only).
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
# The Sera contract each known network's signatures must name. A /config that
# names another contract for one of these chains is refused at bootstrap, so a
# wrong or hostile SERA_BASE_URL cannot point signatures elsewhere.
KNOWN_SERA_CONTRACTS = {
    1: "0xB5C50C5D5f038404F85970b7f5B7259C4AC0E198",  # Ethereum mainnet
    11155111: "0x83475A1bD98a8DC2DCd507A747e4DC85da241D6e",  # Sepolia testnet
}
# Sera's documented per-wallet limits for API-key requests, in requests per second.
RATE_LIMITS = {"read": 10.0, "trade": 5.0, "cancel": 2.0}
# A rate-limited (429) or server-error (503) reply is retried this many times.
MAX_RETRIES = 2
# The longest Retry-After wait honoured before a retry, in seconds.
MAX_RETRY_WAIT_S = 5.0
# POSTs that are safe to repeat after a server error: Sera dedupes an order on
# its client-chosen order_id, and a repeated cancel changes nothing.
# A VL batch is not here: Sera does not document deduping a whole batch.
_REPEATABLE_POSTS = {"/orders", "/orders/cancel", "/orders/vl/cancel"}


@dataclass(frozen=True)
class MarketInfo:
    """Per-market metadata from `GET /markets`."""

    # Sera market label, for example XSGD/USDC.
    symbol: str
    # Token symbols as returned by Sera.
    base_symbol: str
    quote_symbol: str
    # ERC-20/token addresses used in signed order payloads.
    base_address: str
    quote_address: str
    # Token decimal scales for raw amount conversions.
    base_decimals: int
    quote_decimals: int
    # Fallback precision metadata when exact step sizes are absent.
    tick_precision: int
    quantity_precision: int
    # Optional exact price and amount grids.
    price_step: Decimal | None = None
    amount_step: Decimal | None = None
    # Server-provided rounding label, retained for visibility.
    rounding_mode: str = ""
    # Sera's bid minimum is quote-token notional.
    min_bid_quote_amount: Decimal = Decimal(0)
    # Sera's ask minimum is base-token amount.
    min_ask_amount: Decimal = Decimal(0)


@dataclass(frozen=True)
class OrderRecord:
    """Server-acknowledged order identifier."""

    # Public order UUID string.
    order_id: str
    # Packed uint256 identifier used for signing/cancellation.
    uuid_int: int


@dataclass(frozen=True)
class VLLeg:
    """Unsigned order intent for one VL batch sibling."""

    # Market label for this sibling order.
    market: str
    # Sera side string, normally "buy"/"sell" or API-equivalent.
    side: str
    # Base-token quantity in human decimals.
    qty_base: Decimal
    # Limit price in quote-per-base human decimals.
    price: Decimal
    # Unix-seconds order expiry.
    expiration: int


@dataclass(frozen=True)
class VLBatchAmendment:
    """Server-side quantity clip for one accepted VL sibling."""

    # Order id of the leg Sera amended.
    order_id: str
    # Amount sent by Rotor.
    original_amount: str
    # Amount accepted by Sera after clipping.
    actual_amount: str
    # Sera-provided explanation for the amendment.
    reason: str


@dataclass(frozen=True)
class VLCancelledLeg:
    """VL sibling dropped by the matching engine at placement."""

    # Order id of the leg Sera cancelled.
    order_id: str
    # Sera-provided cancellation reason.
    reason: str


@dataclass(frozen=True)
class VLGroup:
    """Authoritative shared-budget snapshot returned by Sera."""

    # Primary order id identifying the VL group.
    primary_id: str
    # Maximum shared spend budget for the group.
    max_budget: str
    # Budget consumed so far, as reported by Sera.
    budget_consumed: str
    # Token symbol/address used for spend accounting.
    spent_token: str


@dataclass(frozen=True)
class VLBatchResult:
    """Server-acknowledged VL batch placement result."""

    # Batch identifier used for later cancellation.
    vl_batch_id: str
    # Order ids returned by Sera for each submitted leg.
    order_ids: list[str]
    # Order ids plus uuid_int values retained for local tracking.
    records: list[OrderRecord]
    # Quantity clips returned by Sera.
    amendments: list[VLBatchAmendment]
    # Legs cancelled immediately by Sera.
    cancelled: list[VLCancelledLeg]
    # Immediate fills returned in the placement response.
    fills: list[dict]
    # Optional authoritative VL budget state.
    vl_group: VLGroup | None


@dataclass(frozen=True)
class SignableOrder:
    """A limit order rotor built itself: the Order message it signs, and its wire form."""

    # UUID string Rotor generated or reused for this order.
    order_id: str
    # Packed identifier derived from the order/group/executor values.
    uuid_int: int
    # The EIP-712 Order message (eip712.ORDER_TYPES) rotor signs.
    struct: dict
    # Wire payload Rotor submits after attaching a signature.
    wire: dict


# Signer contract: domain + type schema + message -> 0x-prefixed signature.
Signer = Callable[[dict, dict, dict], str]


class SeraClient:
    """REST wrapper for Sera order, VL batch, fills, and balance endpoints."""

    def __init__(
        self,
        *,
        owner_address: str,
        signer: Signer,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.testnet.sera.cx/api/v1",
        timeout: float = 10.0,
        log: logging.Logger | None = None,
    ) -> None:
        """Create an HTTP client wrapper; call `bootstrap()` before trading."""
        # Sera expects lower-case owner addresses in wire payloads.
        self.owner_address = owner_address.lower()
        # Signing is injected so tests/dry-runs can avoid real private keys.
        self._signer = signer
        # API credentials are stored for authenticated endpoints.
        self._api_key = api_key
        self._api_secret = api_secret
        # Normalize the base URL once so later path joins are simple.
        self.base_url = base_url.rstrip("/")
        # Refuse to sign and submit orders over a non-TLS transport (outside of
        # localhost dev/mocks): an http:// or MITM'd endpoint could feed rotor
        # false market metadata, signing domain, or server time.
        _require_secure_base_url(self.base_url)
        # A single httpx client keeps connection behavior consistent.
        self._http = httpx.Client(timeout=timeout)
        # Next free request slot per rate-limit group (monotonic seconds).
        self._next_slot: dict[str, float] = {}
        # Swappable so tests can run without real waits.
        self._sleep = time.sleep
        self._monotonic = time.monotonic
        # Use caller logger when provided; otherwise fall back to module logger.
        self._log = log or logging.getLogger(__name__)

        # Populated by bootstrap from Sera's public config endpoint.
        self.eip712_domain: dict | None = None
        # Executor id affects uuid_int packing and comes from /health.
        self.executor_id: int = 0
        # Server-provided VL batch size limits; defaults are conservative.
        self.vl_batch_min: int = 2
        self.vl_batch_max: int = 50
        # Market metadata cache keyed by Sera symbol.
        self.markets_by_symbol: dict[str, MarketInfo] = {}

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        # Idempotent and safe to call from a runner's shutdown path.
        self._http.close()

    def __enter__(self) -> SeraClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def bootstrap(self) -> None:
        """Fetch public config, executor id, and market metadata."""
        # `/config` provides signing-domain data and public limits.
        cfg = self._request("GET", "/config", authed=False)
        # Prefer the full server-provided domain, but tolerate older configs
        # that expose only chain_id and sera_address.
        server_domain = cfg.get("eip712_domain")
        domain = server_domain or {
            "name": "Sera",
            "version": "1",
            "chainId": cfg.get("chain_id"),
            "verifyingContract": cfg.get("sera_address"),
        }
        if not server_domain:
            # If the real on-chain domain name/version differs from these
            # hardcoded literals, every signature mis-verifies (orders silently
            # never place). Surface that we are guessing the domain separator.
            self._log.warning(
                "GET /config omitted eip712_domain; assuming name=Sera version=1 "
                "(chainId=%s verifyingContract=%s) — verify against the on-chain domain",
                domain.get("chainId"),
                domain.get("verifyingContract"),
            )
        # Both fields are mandatory for EIP-712 signature verification.
        if not domain.get("chainId") or not domain.get("verifyingContract"):
            raise RuntimeError(f"GET /config returned incomplete domain: {cfg!r}")
        expected = KNOWN_SERA_CONTRACTS.get(int(domain["chainId"]))
        if expected is None:
            self._log.warning(
                "chain %s is not a known Sera network; signing for contract %s from GET /config",
                domain["chainId"], domain["verifyingContract"],
            )
        elif str(domain["verifyingContract"]).lower() != expected.lower():
            raise RuntimeError(
                f"GET /config names Sera contract {domain['verifyingContract']} for chain "
                f"{domain['chainId']}, not {expected}; check SERA_BASE_URL"
            )
        self.eip712_domain = domain
        # VL batch limits are dynamic server config; keep local guards in sync.
        vl_limits = (cfg.get("limits") or {}).get("vl_batch") or {}
        self.vl_batch_min = int(vl_limits.get("min", self.vl_batch_min))
        self.vl_batch_max = int(vl_limits.get("max", self.vl_batch_max))

        # `/health` exposes the executor id used in uuid_int packing.
        health = self._request("GET", "/health", authed=False)
        self.executor_id = _parse_healthy_executor_id(health)

        # Market metadata drives amount/price grids and signed token addresses.
        markets = self._request("GET", "/markets", authed=False).get("markets", [])
        self.markets_by_symbol = {}
        for m in markets:
            # Parse each row defensively, defaulting optional precision metadata.
            info = MarketInfo(
                symbol=m["symbol"],
                base_symbol=m["base_symbol"],
                quote_symbol=m["quote_symbol"],
                base_address=m["base_address"],
                quote_address=m["quote_address"],
                base_decimals=int(m["base_decimals"]),
                quote_decimals=int(m["quote_decimals"]),
                tick_precision=int(m.get("tick_precision", 6)),
                quantity_precision=int(m.get("quantity_precision", 2)),
                price_step=(
                    Decimal(str(m["price_step"]))
                    if m.get("price_step") is not None else None
                ),
                amount_step=(
                    Decimal(str(m["amount_step"]))
                    if m.get("amount_step") is not None else None
                ),
                rounding_mode=str(m.get("rounding_mode", "")),
                min_bid_quote_amount=Decimal(str(m.get("min_bid_quote_amount", "0"))),
                min_ask_amount=Decimal(str(m.get("min_ask_amount", "0"))),
            )
            # Cache by exact Sera symbol for fast lookup during quoting.
            self.markets_by_symbol[info.symbol] = info
        _check_market_tokens(self.markets_by_symbol, int(domain["chainId"]))

        # Log only public boot metadata; no secrets or signatures appear here.
        self._log.info(
            "sera client booted: chain_id=%s executor_id=%d markets=%d",
            self.eip712_domain.get("chainId"),
            self.executor_id,
            len(self.markets_by_symbol),
        )

    def build_order(
        self,
        market: str,
        side: str,
        qty_base: Decimal,
        price: Decimal,
        expiration: int,
        *,
        order_id: str | None = None,
        leg_id: int = 0,
        group_uuid: str | None = None,
    ) -> SignableOrder:
        """Build a limit order's EIP-712 Order message and wire payload locally.

        Rotor never signs a struct the server hands it: every field of the
        signed message comes from the intended order and the bootstrapped
        market metadata. `eip712.build_order_struct` mirrors the server's own
        builder (golden vectors from Sera's preview endpoint prove it), so the
        server rebuilds the same struct and accepts the signature. The only
        server-supplied inputs left are the token addresses and decimals from
        `GET /markets` and the signing domain from `GET /config`, whose
        contract is pinned for known networks.
        """
        info = self._market(market)
        # VL legs pass their order id and group; standalone orders get a fresh UUID.
        final_order_id = order_id or eip712.new_order_id()
        # Sera requires a packed uuid_int for signing and cancel flows.
        uuid_int = eip712.compose_uuid(
            final_order_id,
            executor_id=self.executor_id,
            leg_id=leg_id,
            group_uuid=group_uuid,
        )
        struct = eip712.build_order_struct(
            owner_address=self.owner_address,
            side=side,
            amount=qty_base,
            price=price,
            base_address=info.base_address,
            quote_address=info.quote_address,
            base_decimals=info.base_decimals,
            quote_decimals=info.quote_decimals,
            uuid_int=uuid_int,
            expiration=expiration,
        )
        wire = _order_wire(
            owner_address=self.owner_address,
            side=side,
            qty_base=qty_base,
            price=price,
            base_address=info.base_address,
            quote_address=info.quote_address,
            order_id=final_order_id,
            uuid_int=uuid_int,
            expiration=expiration,
        )
        return SignableOrder(order_id=final_order_id, uuid_int=uuid_int, struct=struct, wire=wire)

    def place_order(
        self,
        market: str,
        side: str,
        qty_base: Decimal,
        price: Decimal,
        expiration: int,
    ) -> OrderRecord:
        """Build, sign, and place one limit order."""
        order = self.build_order(market, side, qty_base, price, expiration)
        wire = dict(order.wire)
        wire["signature"] = self._sign_order(order)
        body = self._request("POST", "/orders", json_body=wire, expect_status=(200, 201))
        return OrderRecord(
            # Sera may echo a final order id; otherwise keep the generated id.
            order_id=body.get("order_id", order.order_id),
            uuid_int=order.uuid_int,
        )

    def place_vl_batch(self, legs: list[VLLeg]) -> VLBatchResult:
        """Build and sign each sibling, then place one VL batch."""
        # Enforce server batch-size limits locally before generating signatures.
        if len(legs) < self.vl_batch_min:
            raise ValueError(
                f"VL batch requires at least {self.vl_batch_min} legs, got {len(legs)}"
            )
        if len(legs) > self.vl_batch_max:
            raise ValueError(
                f"VL batch cannot exceed {self.vl_batch_max} legs, got {len(legs)}"
            )

        # One UUID anchors the shared VL group; the first leg reuses it as its
        # order id so Sera can identify the primary order.
        group_uuid = eip712.new_order_id()
        orders: list[SignableOrder] = []
        for i, leg in enumerate(legs):
            # Leg 0 is the primary/group id; later legs get independent order ids
            # but share the same group_uuid and carry their leg index.
            orders.append(self.build_order(
                leg.market,
                leg.side,
                leg.qty_base,
                leg.price,
                leg.expiration,
                order_id=group_uuid if i == 0 else eip712.new_order_id(),
                leg_id=i,
                group_uuid=group_uuid,
            ))

        wires: list[dict] = []
        for order in orders:
            # Each sibling carries its own signature over its own Order message.
            wire = dict(order.wire)
            wire["signature"] = self._sign_order(order)
            wires.append(wire)

        # Submit all signed siblings as one VL batch.
        body = self._request(
            "POST",
            "/orders/vl/batch",
            json_body={"orders": wires},
            expect_status=(200, 201),
        )
        # Reconcile server-returned order ids with the locally built orders.
        order_ids = [str(v) for v in body.get("order_ids", [])]
        if not order_ids:
            order_ids = [p.order_id for p in orders]
        # The batch is already live on Sera; resolve a cancelable batch id BEFORE
        # any mismatch handling so a malformed response can never orphan it.
        vl_batch_id = str(
            body.get("vl_batch_id")
            or body.get("batch_id")
            or order_ids[0]
            or orders[0].order_id
        )
        # A length mismatch breaks positional id->leg tracking, but the whole
        # batch is still cancelable by vl_batch_id. Log loudly and track what we
        # can correlate rather than raising and leaving live orders unmanaged.
        if len(order_ids) != len(orders):
            self._log.error(
                "POST /orders/vl/batch returned %d order_ids for %d submitted legs; "
                "tracking batch %s for cancellation",
                len(order_ids),
                len(orders),
                vl_batch_id,
            )
        # Pair returned order ids with the uuid_int values computed for signing
        # (positionally, over the safely-correlated prefix on a mismatch).
        records = [
            OrderRecord(order_id=order_id, uuid_int=order.uuid_int)
            for order_id, order in zip(order_ids, orders, strict=False)
        ]
        # Parse optional shared-budget state into a typed dataclass.
        vl_group = _parse_vl_group(body.get("vl_group"))
        return VLBatchResult(
            vl_batch_id=vl_batch_id,
            order_ids=order_ids,
            records=records,
            amendments=[
                VLBatchAmendment(
                    order_id=str(row.get("order_id", "")),
                    original_amount=str(row.get("original_amount", "")),
                    actual_amount=str(row.get("actual_amount", "")),
                    reason=str(row.get("reason", "")),
                )
                for row in body.get("amendments", []) or []
            ],
            cancelled=[
                VLCancelledLeg(
                    order_id=str(row.get("order_id", "")),
                    reason=str(row.get("reason", "")),
                )
                for row in body.get("cancelled", []) or []
            ],
            fills=list(body.get("fills", []) or []),
            vl_group=vl_group,
        )

    def cancel_order(self, order_id: str, uuid_int: int) -> bool:
        """Cancel one order. Returns False when Sera reports cancel cooldown."""
        # Cancellation signatures use the packed uuid_int, not the UUID string.
        signature = self._sign(
            eip712.CANCEL_ORDER_TYPES,
            {"owner": self.owner_address, "orderId": int(uuid_int)},
        )
        # Include both ids because Sera's REST endpoint accepts the public id and
        # verifies the signed packed id.
        wire = {
            "owner_address": self.owner_address,
            "order_id": order_id,
            "uuid_int": str(uuid_int),
            "signature": signature,
        }
        try:
            # 204 is an allowed empty success response.
            self._request("POST", "/orders/cancel", json_body=wire, expect_status=(200, 204))
            return True
        except errors.SeraCancelCooldown:
            # Runner can keep tracking the order and retry later.
            return False

    def cancel_vl_batch(self, vl_batch_id: str) -> bool:
        """Cancel an entire VL batch by primary order id."""
        # VL batch cancellation signs the string batch id.
        signature = self._sign(
            eip712.CANCEL_VL_BATCH_TYPES,
            {"owner": self.owner_address, "vlBatchId": str(vl_batch_id)},
        )
        # Keep the wire key names aligned with Sera's cancel endpoint.
        wire = {
            "owner_address": self.owner_address,
            "vl_batch_id": str(vl_batch_id),
            "signature": signature,
        }
        try:
            self._request(
                "POST",
                "/orders/vl/cancel",
                json_body=wire,
                expect_status=(200, 204),
            )
            return True
        except errors.SeraCancelCooldown:
            # Same cooldown behavior as single-order cancellation.
            return False

    def get_config_limits(self) -> dict[str, dict[str, int]]:
        """Return public API limits, refreshing from `GET /config`."""
        # Refreshing limits lets long-running clients adapt to server changes.
        cfg = self._request("GET", "/config", authed=False)
        limits = dict(cfg.get("limits") or {})
        vl_limits = dict(limits.get("vl_batch") or {})
        if vl_limits:
            # Update local guards and return normalized ints to callers.
            self.vl_batch_min = int(vl_limits.get("min", self.vl_batch_min))
            self.vl_batch_max = int(vl_limits.get("max", self.vl_batch_max))
            limits["vl_batch"] = {
                "min": self.vl_batch_min,
                "max": self.vl_batch_max,
            }
        return limits

    def get_server_time(self) -> int:
        """Return Sera's current Unix timestamp from `GET /system/time`."""
        body = self._request("GET", "/system/time", authed=False)
        try:
            return int(body["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"GET /system/time returned invalid timestamp: {body!r}") from exc

    def expiration_from_server_time(self, ttl_seconds: int) -> int:
        """Return a signed-payload expiration derived from Sera server time."""
        return self.get_server_time() + int(ttl_seconds)

    def get_vault_balances(self) -> dict[str, Decimal]:
        """Return each token's vault total (available + frozen) by symbol, in token units.

        Orders spend vault funds, so this is what can back them. Frozen funds
        count because they are mostly held by rotor's own resting orders, which
        each tick cancels and replaces. Tokens with no balance are omitted.
        """
        body = self._request(
            "GET", "/balances", params={"owner_address": self.owner_address})
        out: dict[str, Decimal] = {}
        for row in body.get("balances") or []:
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                out[symbol] = (
                    Decimal(str(row["vault_total"])) / (Decimal(10) ** int(row["decimals"]))
                )
        return out

    def get_open_orders(self, status: str = "pending") -> list[dict]:
        """List current open orders for this wallet."""
        # Authenticated endpoint scoped by owner address and status.
        body = self._request(
            "GET",
            "/orders",
            params={"owner_address": self.owner_address, "status": status, "limit": 500},
        )
        # Older/newer API responses may use trades or orders.
        return list(body.get("trades") or body.get("orders") or [])

    def get_order_fills(
        self, order_id: str, *, page_limit: int = 100, max_pages: int = 50
    ) -> list[dict]:
        """List all fills for one order, following pagination.

        A high-fill order can exceed one page; stopping at the first page would
        undercount the filled quantity and prevent the runner from ever pruning
        a fully-filled order. Walk pages until a short page (or the safety cap).
        """
        out: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            body = self._request(
                "GET",
                f"/fills/{order_id}",
                params={"limit": page_limit, "offset": offset},
            )
            # Missing items is treated as no fills rather than an error.
            items = list(body.get("items") or [])
            out.extend(items)
            if len(items) < page_limit:
                return out
            offset += page_limit
        # Reaching the cap means there may be more fills than we paged; surface it
        # so an operator can investigate rather than silently undercounting.
        self._log.warning(
            "get_order_fills hit max_pages=%d for order %s (%d fills read)",
            max_pages,
            order_id,
            len(out),
        )
        return out

    def _market(self, symbol: str) -> MarketInfo:
        """Return cached market metadata or fail if bootstrap has not loaded it."""
        info = self.markets_by_symbol.get(symbol)
        if info is None:
            raise KeyError(f"market not in cache: {symbol!r}; call bootstrap()")
        return info

    def _sign(self, types: dict, message: dict) -> str:
        """Sign typed data using the bootstrapped EIP-712 domain."""
        # The domain comes from Sera config, so signing before bootstrap is unsafe.
        if self.eip712_domain is None:
            raise RuntimeError("client not booted; call bootstrap() first")
        return self._signer(self.eip712_domain, types, message)

    def _sign_order(self, order: SignableOrder) -> str:
        """Sign a locally built order as Sera's Order type."""
        return self._sign(
            eip712.ORDER_TYPES,
            _typed_message_for_signing(eip712.ORDER_TYPES, order.struct),
        )

    def _pace(self, group: str) -> None:
        """Wait until a request in `group` stays within Sera's per-wallet limit."""
        now = self._monotonic()
        slot = max(now, self._next_slot.get(group, now))
        self._next_slot[group] = slot + 1.0 / RATE_LIMITS[group]
        if slot > now:
            self._sleep(slot - now)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        authed: bool = True,
        expect_status: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """Make one Sera HTTP request and normalize success/error responses."""
        # Join pre-normalized base URL with the endpoint path.
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if authed:
            # Sera uses a bearer value containing key and secret.
            headers["Authorization"] = f"Bearer {self._api_key}:{self._api_secret}"
        kwargs: dict[str, Any] = {"params": params}
        if json_body is not None:
            # Compact JSON bytes keep signatures/payloads deterministic enough for
            # API transport while avoiding whitespace churn.
            headers["Content-Type"] = "application/json"
            kwargs["content"] = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        # API-key requests count against Sera's per-wallet limits; public ones
        # are throttled at Sera's edge instead.
        group = _rate_group(method, path) if authed else None
        for attempt in range(MAX_RETRIES + 1):
            if group is not None:
                self._pace(group)
            response = self._http.request(method, url, headers=headers, **kwargs)
            if response.status_code in expect_status:
                break
            try:
                # Prefer structured errors when the server returns JSON.
                body = response.json()
            except Exception:
                # Preserve text bodies in the same envelope shape.
                body = {"detail": response.text}
            error = errors.from_response(response.status_code, body)
            if attempt == MAX_RETRIES or not _retryable(error, method, path):
                raise error
            wait = _retry_wait(response, attempt)
            self._log.debug("retrying %s %s in %.1fs after %s", method, path, wait, error)
            self._sleep(wait)
        # Empty success responses normalize to an empty dict for callers.
        if response.status_code == 204 or not response.content:
            return {}
        try:
            # Most successful Sera endpoints return JSON bodies.
            return response.json()
        except Exception:
            # Treat a non-JSON success body as an empty object to keep callers
            # defensive.
            return {}


def _order_wire(
    *,
    owner_address: str,
    side: str,
    qty_base: Decimal,
    price: Decimal,
    base_address: str,
    quote_address: str,
    order_id: str,
    uuid_int: int,
    expiration: int,
) -> dict:
    """Build the unsigned order payload rotor signs and posts to Sera."""
    # Sera accepts human-decimal amount/price strings and token addresses.
    return {
        # Owner and side identify who trades and direction.
        "owner_address": owner_address,
        "side": side,
        # Human-decimal inputs are stringified to preserve Decimal precision.
        "amount": str(qty_base),
        "price": str(price),
        # Rotor only places limit orders.
        "order_type": "limit",
        # Market token addresses identify the pair; Sera interprets direction
        # from the side field.
        "from_address": base_address,
        "to_address": quote_address,
        # Identifiers and expiry match the signed Order message.
        "order_id": order_id,
        "uuid_int": str(uuid_int),
        "expiration": int(expiration),
    }


def _parse_healthy_executor_id(health: Any) -> int:
    """Validate `GET /health` and return the executor id used for UUID packing."""
    if not isinstance(health, dict):
        raise RuntimeError(f"GET /health returned invalid body: {health!r}")

    status = str(health.get("status") or "").lower()
    if status and status != "healthy":
        raise RuntimeError(f"GET /health is not healthy: {health!r}")
    if health.get("signature_ready") is False:
        raise RuntimeError(f"GET /health reports signature verification is not ready: {health!r}")

    raw_executor = health.get("executor_id")
    if raw_executor is None:
        raise RuntimeError(f"GET /health omitted executor_id: {health!r}")
    try:
        executor_id = int(raw_executor)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"GET /health returned invalid executor_id: {health!r}") from exc
    if not 0 <= executor_id <= eip712.EXECUTOR_MAX:
        raise RuntimeError(f"GET /health executor_id out of range: {health!r}")

    relayer_executor = health.get("relayer_executor_id")
    if relayer_executor not in (None, ""):
        try:
            relayer_executor_id = int(relayer_executor)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"GET /health returned invalid relayer_executor_id: {health!r}"
            ) from exc
        if relayer_executor_id != executor_id:
            raise RuntimeError(f"GET /health executor drift: {health!r}")

    return executor_id


def _typed_message_for_signing(types: dict, message: dict) -> dict:
    """Coerce JSON decimal strings into ints for integer typed-data fields."""
    # Copy so callers retain the original server payload for debugging.
    out = dict(message)
    field_types = {}
    for fields in types.values():
        for field in fields:
            # Flatten all type schemas into a field-name -> EIP type map.
            field_types[field.get("name")] = field.get("type", "")
    for key, typ in field_types.items():
        if key not in out:
            continue
        # Leave array/tuple typed fields for eth-account's own ABI coercion.
        if "[" in typ:
            continue
        # eth-account expects Python ints for uint/int fields, while HTTP JSON
        # may carry those values as decimal or hex strings.
        if typ.startswith(("uint", "int")):
            out[key] = _coerce_int(out[key])
    return out


def _coerce_int(value: Any) -> int:
    """Coerce a typed-data integer field value to int, tolerating hex strings."""
    if isinstance(value, bool):
        # bool is an int subclass; preserve it rather than mangling to 0/1.
        return value
    if isinstance(value, str):
        text = value.strip()
        if text[:2].lower() == "0x":
            return int(text, 16)
        return int(text, 10)
    return int(value)


def _check_market_tokens(markets: dict[str, MarketInfo], chain_id: int) -> None:
    """Refuse market metadata that disagrees with the tokens pinned for this chain.

    Rotor signs orders for the token addresses and decimals GET /markets gives.
    A wrong or hostile endpoint could otherwise swap one listed token for
    another, so each market's symbol must match its two tokens, a pinned symbol
    must have its pinned address and decimals, and a pinned address must not
    appear under another symbol. Tokens not pinned are accepted as reported.
    """
    known = KNOWN_TOKENS.get(chain_id)
    if known is None:
        return
    symbol_of = {address: symbol for symbol, (address, _) in known.items()}
    for info in markets.values():
        if info.symbol.upper() != f"{info.base_symbol}/{info.quote_symbol}".upper():
            raise RuntimeError(
                f"GET /markets lists {info.symbol} with tokens "
                f"{info.base_symbol}/{info.quote_symbol}; check SERA_BASE_URL"
            )
        for symbol, address, decimals in (
            (info.base_symbol, info.base_address, info.base_decimals),
            (info.quote_symbol, info.quote_address, info.quote_decimals),
        ):
            symbol, address = symbol.upper(), address.lower()
            pinned = known.get(symbol)
            if pinned is not None and pinned != (address, decimals):
                raise RuntimeError(
                    f"GET /markets lists {symbol} at {address} with {decimals} decimals, "
                    f"not {pinned[0]} with {pinned[1]}; check SERA_BASE_URL"
                )
            if symbol_of.get(address, symbol) != symbol:
                raise RuntimeError(
                    f"GET /markets lists {symbol_of[address]}'s address {address} as "
                    f"{symbol}; check SERA_BASE_URL"
                )


def _rate_group(method: str, path: str) -> str:
    """Return the Sera rate-limit group an API-key request counts against."""
    if method == "GET":
        return "read"
    return "cancel" if path.endswith("/cancel") else "trade"


def _retryable(error: errors.SeraError, method: str, path: str) -> bool:
    """Return whether a failed request is safe and useful to send again."""
    if isinstance(error, errors.SeraRateLimit):
        # Rejected by the rate limiter before Sera acted on it.
        return True
    # A server error may come after Sera acted, so only repeat requests whose
    # repetition is harmless.
    return error.status_code == 503 and (method == "GET" or path in _REPEATABLE_POSTS)


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before a retry: Retry-After if given, else backoff."""
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:
        wait = float(2**attempt)
    return min(max(wait, 0.0), MAX_RETRY_WAIT_S)


def _require_secure_base_url(base_url: str) -> None:
    """Reject a non-TLS Sera base URL except for localhost dev/mocks."""
    scheme = urlsplit(base_url).scheme.lower()
    host = urlsplit(base_url).hostname or ""
    if scheme == "https":
        return
    if scheme == "http" and host in _LOCAL_HOSTS:
        return
    raise ValueError(
        "SERA_BASE_URL must use https:// (the maker signs orders against this "
        f"endpoint); got {base_url!r}"
    )


def _parse_vl_group(value) -> VLGroup | None:
    """Parse optional VL group metadata from a Sera response."""
    # Missing/malformed group data is optional and should not fail placement.
    if not isinstance(value, dict):
        return None
    return VLGroup(
        primary_id=str(value.get("primary_id", "")),
        max_budget=str(value.get("max_budget", "")),
        budget_consumed=str(value.get("budget_consumed", "")),
        spent_token=str(value.get("spent_token", "")),
    )
