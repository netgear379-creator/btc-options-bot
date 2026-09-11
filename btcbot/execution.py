"""Order construction, submission and fill management.

Fixes the two failure modes that meant the old bot never opened a single
intended trade in two hours of trying:

1. "insufficient options buying power" -- it sized against `cash` while Alpaca
   margins options against `options_buying_power`, which was 2.5% of cash.
2. "position intent mismatch, inferred: sell_to_close, specified: sell_to_open"
   -- it tried to sell-to-open a strike it was already long from an earlier
   spread. Intent is now derived from the actual net position per contract.

It also stops the bot from re-submitting a rejected order every 30 seconds
forever, which is what the old log is almost entirely made of.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (OrderClass, OrderSide, OrderStatus,
                                  PositionIntent, TimeInForce)
from alpaca.trading.requests import (GetOrdersRequest, LimitOrderRequest,
                                     OptionLegRequest)

from . import config

log = logging.getLogger(__name__)

TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
            OrderStatus.REJECTED, OrderStatus.DONE_FOR_DAY}


def round_tick(price: float) -> float:
    return round(round(price / config.ORDER_TICK) * config.ORDER_TICK, 2)


class Executor:
    def __init__(self, trading_client: TradingClient = None):
        self.trading = trading_client or TradingClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=config.ALPACA_PAPER)
        # symbol -> consecutive rejections, so we stop hammering a bad order
        self._rejections: dict = {}

    # ---- broker state ---------------------------------------------------- #
    def net_positions(self) -> dict:
        """symbol -> signed contract quantity."""
        out = {}
        for p in self.trading.get_all_positions():
            try:
                out[p.symbol] = float(p.qty)
            except (TypeError, ValueError):
                continue
        return out

    def account_snapshot(self):
        a = self.trading.get_account()
        return a

    def _intent(self, symbol: str, side: str, net: dict) -> PositionIntent:
        """Derive open/close intent from the net position we actually hold.

        Alpaca infers intent itself and rejects the order when our declared
        intent disagrees. Selling a contract we are long is a close, full stop,
        regardless of what the strategy thinks it is doing.
        """
        held = net.get(symbol, 0.0)
        if side == "sell":
            return PositionIntent.SELL_TO_CLOSE if held > 0 else PositionIntent.SELL_TO_OPEN
        return PositionIntent.BUY_TO_CLOSE if held < 0 else PositionIntent.BUY_TO_OPEN

    # ---- opening --------------------------------------------------------- #
    def open_spread(self, candidate, qty: int, dry_run: bool = False):
        """Submit the multi-leg credit order. Returns the order, or None."""
        key = "|".join(sorted(candidate.all_symbols))
        if self._rejections.get(key, 0) >= 3:
            log.warning("skipping %s: rejected %d times already this session",
                        key, self._rejections[key])
            return None

        net = self.net_positions()
        conflict = [s for s in candidate.all_symbols if s in net]
        if conflict:
            log.warning("refusing to open: already hold %s at the broker", conflict)
            return None

        legs = [OptionLegRequest(
            symbol=l.symbol, ratio_qty=1,
            side=OrderSide.SELL if l.side == "sell" else OrderSide.BUY,
            position_intent=self._intent(l.symbol, l.side, net))
            for l in candidate.legs]

        limit_price = round_tick(candidate.credit)
        if limit_price <= 0:
            log.warning("refusing to open: non-positive limit price")
            return None

        log.info("ORDER OPEN  %s", candidate.describe())
        log.info("            qty=%d  credit=$%.2f  max_profit=$%.0f  max_risk=$%.0f",
                 qty, limit_price, candidate.max_profit * qty, candidate.max_loss * qty)
        if dry_run:
            log.info("            [dry-run] not submitted")
            return None

        req = LimitOrderRequest(
            order_class=OrderClass.MLEG, qty=qty,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price, legs=legs)
        try:
            order = self.trading.submit_order(req)
        except Exception as exc:
            self._rejections[key] = self._rejections.get(key, 0) + 1
            log.error("open rejected (%d/3): %s", self._rejections[key], exc)
            return None

        self._rejections.pop(key, None)
        log.info("            submitted id=%s status=%s", order.id, order.status)
        return self.await_fill(order)

    # ---- closing --------------------------------------------------------- #
    def close_spread(self, position, limit_debit: float = None, dry_run: bool = False):
        """Close every leg of one spread as a single multi-leg order, so the
        legs cannot be left half-unwound."""
        net = self.net_positions()
        legs = []
        for raw in position.legs:
            leg = raw if isinstance(raw, dict) else raw.__dict__
            symbol = leg["symbol"]
            if symbol not in net:
                log.warning("leg %s no longer at broker; closing what remains", symbol)
                continue
            # reverse of how it was opened
            side = "buy" if leg["side"] == "sell" else "sell"
            legs.append(OptionLegRequest(
                symbol=symbol, ratio_qty=1,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                position_intent=self._intent(symbol, side, net)))

        if not legs:
            log.info("nothing left to close for %s", position.id)
            return None
        if dry_run:
            log.info("[dry-run] would close %s at debit $%.2f", position.id, limit_debit or 0)
            return None

        # A closing debit is submitted as a negative limit price on a credit
        # structure; Alpaca's MLEG convention is price from our side of the
        # trade, so paying a debit means a negative net price.
        try:
            if limit_debit is None:
                order = None
                for leg in legs:                    # fallback: leg-by-leg market close
                    order = self.trading.close_position(symbol_or_asset_id=leg.symbol)
                log.info("closed %s leg-by-leg", position.id)
                return order

            req = LimitOrderRequest(
                order_class=OrderClass.MLEG, qty=position.qty,
                time_in_force=TimeInForce.DAY,
                limit_price=round_tick(-abs(limit_debit)), legs=legs)
            order = self.trading.submit_order(req)
            log.info("ORDER CLOSE %s id=%s debit=$%.2f", position.id, order.id, abs(limit_debit))
            return self.await_fill(order)
        except Exception as exc:
            log.error("close failed for %s: %s", position.id, exc)
            return None

    def force_close(self, position):
        """Last resort: market-close each leg individually."""
        results = []
        for raw in position.legs:
            leg = raw if isinstance(raw, dict) else raw.__dict__
            try:
                results.append(self.trading.close_position(symbol_or_asset_id=leg["symbol"]))
            except Exception as exc:
                log.error("force close of %s failed: %s", leg["symbol"], exc)
        return results

    # ---- fill handling ---------------------------------------------------- #
    def await_fill(self, order, timeout_s: int = None):
        """Poll to a terminal state, then cancel if still resting.

        A credit spread left working at a stale limit is an unhedged intention,
        not a position. We either get filled inside the window or we walk away.
        """
        timeout_s = timeout_s or config.ORDER_FILL_TIMEOUT_S
        deadline = time.time() + timeout_s
        latest = order

        while time.time() < deadline:
            time.sleep(3)
            try:
                latest = self.trading.get_order_by_id(order.id)
            except Exception as exc:
                log.warning("order poll failed: %s", exc)
                continue
            if latest.status in TERMINAL:
                break
            if latest.status == OrderStatus.PARTIALLY_FILLED:
                log.info("            partial fill %s/%s", latest.filled_qty, latest.qty)

        if latest.status == OrderStatus.FILLED:
            log.info("            FILLED %s @ %s", latest.filled_qty, latest.filled_avg_price)
            return latest

        if latest.status not in TERMINAL:
            try:
                self.trading.cancel_order_by_id(order.id)
                log.info("            no fill in %ds -- cancelled %s", timeout_s, order.id)
            except Exception as exc:
                log.warning("cancel failed for %s: %s", order.id, exc)
            return None

        log.info("            ended %s", latest.status)
        return latest if latest.status == OrderStatus.FILLED else None

    def cancel_stale_orders(self) -> int:
        """Sweep anything we left resting from a previous run."""
        try:
            open_orders = self.trading.get_orders(GetOrdersRequest(status="open"))
        except Exception as exc:
            log.warning("could not list open orders: %s", exc)
            return 0
        n = 0
        for o in open_orders:
            age = (datetime.now(timezone.utc) - o.submitted_at).total_seconds()
            if age > config.ORDER_FILL_TIMEOUT_S:
                try:
                    self.trading.cancel_order_by_id(o.id)
                    n += 1
                except Exception as exc:
                    log.warning("cancel %s failed: %s", o.id, exc)
        if n:
            log.info("cancelled %d stale order(s)", n)
        return n
