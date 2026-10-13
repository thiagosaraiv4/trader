"""
core/maker_sim.py
-----------------
Paper market maker (one side at a time) driven by the live best bid/ask
(bookTicker) and trade prints. No exchange, no money: it only answers "would
passive limit orders at the top of the book make money after the fact?".

Cycle:
    FLAT  → keep a BUY limit at the best bid (re-quoted when the bid moves)
    LONG  → keep a SELL limit at max(best ask, entry + 1 tick); exit with a
            taker SELL at the best bid if the mid falls stop_pct below the
            entry or the position is older than max_hold_sec

Fill model (conservative):
    - An order rests at the BACK of the queue: the size already shown at its
      price when it becomes active must trade first.
    - Only trades by the opposite taker count (a seller hitting bids for our
      BUY, a buyer lifting offers for our SELL). A trade at our price eats the
      queue; a trade through our price (below a bid / above an ask) fills us.
    - Every action (place, cancel) takes `latency_sec` to reach the exchange,
      in the clock of the events as we receive them. A cancelled order can
      still fill until its cancel lands.
    - Re-quotes are throttled (min_requote_sec) to stay far from Binance's
      unfilled-order limit (50 per 10 s).

Adverse selection is measured after every BUY fill: the mid price 1/5/30/60 s
later relative to the fill price. Consistently negative = the fills happen
right before the price drops — the classic way market makers lose.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

MARKOUTS = (1, 5, 30, 60)   # seconds after a BUY fill


@dataclass
class Order:
    side: str                   # "buy" | "sell"
    price: float
    qty: float
    placed_at: float            # decision time
    active_at: float            # reaches the book
    cancel_at: float = math.inf
    queue_ahead: float | None = None   # set when it becomes active
    consumed: float = 0.0


@dataclass
class RoundTrip:
    entry_time: float
    entry_price: float
    exit_time: float
    exit_price: float
    qty: float
    exit_kind: str              # "maker" | "stop" | "timeout" | "shutdown"
    fees: float
    pnl: float                  # net of fees, quote currency
    markouts: dict[int, float] = field(default_factory=dict)  # s -> mid move / fill price


class MakerSim:
    def __init__(
        self,
        notional: float = 1_000.0,
        maker_fee: float = 0.0,
        taker_fee: float = 0.001,
        tick: float = 0.01,
        latency_sec: float = 0.3,
        min_requote_sec: float = 0.5,
        stop_pct: float = 0.002,
        max_hold_sec: float = 300.0,
        on_round_trip: Callable[[RoundTrip], None] | None = None,
        on_fill: Callable[[str, float, float, float], None] | None = None,
    ) -> None:
        self.notional = notional
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.tick = tick
        self.latency = latency_sec
        self.min_requote = min_requote_sec
        self.stop_pct = stop_pct
        self.max_hold = max_hold_sec
        self._on_round_trip = on_round_trip
        self._on_fill = on_fill

        self.bid = self.ask = math.nan
        self.bid_size = self.ask_size = 0.0
        self.orders: list[Order] = []
        self._last_quote = -math.inf
        # Position (long only)
        self.qty = 0.0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_fee = 0.0
        # Markouts of the open position; (due time, seconds, fill price, dict)
        self._marks: dict[int, float] = {}
        self._pending_marks: list[tuple[float, int, float, dict[int, float]]] = []
        # Closed trips whose markouts are still being measured
        self._awaiting: list[RoundTrip] = []
        self.orders_placed = 0
        self.round_trips: list[RoundTrip] = []

    # -- helpers -----------------------------------------------------------

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def flat(self) -> bool:
        return self.qty == 0

    def _live(self, now: float) -> list[Order]:
        return [o for o in self.orders if o.active_at <= now < o.cancel_at]

    def _wanted(self) -> tuple[str, float] | None:
        if math.isnan(self.bid) or math.isnan(self.ask) or self.bid >= self.ask:
            return None
        if self.flat:
            return "buy", self.bid
        return "sell", max(self.ask, self.entry_price + self.tick)

    def _activate(self, now: float) -> None:
        """Orders reaching the book join the back of the queue at their price."""
        for o in self.orders:
            if o.queue_ahead is not None or o.active_at > now:
                continue
            crosses = o.price >= self.ask if o.side == "buy" else o.price <= self.bid
            if crosses:
                # Post-only (LIMIT_MAKER): Binance rejects it instead of
                # executing as a taker
                o.cancel_at = now
                self._last_quote = -math.inf
                continue
            touch, size = (self.bid, self.bid_size) if o.side == "buy" else (self.ask, self.ask_size)
            # Behind the touch: unknown queue, fills only if traded through
            o.queue_ahead = size if o.price == touch else (
                0.0 if (o.price > touch if o.side == "buy" else o.price < touch) else math.inf)
        self.orders = [o for o in self.orders if now < o.cancel_at]

    def _quote(self, now: float) -> None:
        want = self._wanted()
        pending = [o for o in self.orders if o.cancel_at == math.inf]
        if want is None:
            return
        side, price = want
        if pending and pending[-1].side == side and pending[-1].price == price:
            return
        if now - self._last_quote < self.min_requote:
            return
        for o in pending:
            o.cancel_at = now + self.latency
        qty = round(self.notional / price, 5) if side == "buy" else self.qty
        if qty <= 0:
            return
        self.orders.append(Order(side, price, qty, now, now + self.latency))
        self.orders_placed += 1
        self._last_quote = now

    def _fill(self, o: Order, now: float) -> None:
        fee = o.price * o.qty * self.maker_fee
        self.orders = [x for x in self.orders if x is not o]
        for x in self.orders:          # one side at a time: drop the rest
            x.cancel_at = min(x.cancel_at, now + self.latency)
        if self._on_fill:
            self._on_fill(o.side, o.price, o.qty, now)
        if o.side == "buy":
            self.qty, self.entry_price, self.entry_time, self.entry_fee = o.qty, o.price, now, fee
            self._marks = {}
            for s in MARKOUTS:
                self._pending_marks.append((now + s, s, o.price, self._marks))
        else:
            self._close(now, o.price, fee, "maker")

    def _close(self, now: float, price: float, exit_fee: float, kind: str) -> None:
        gross = (price - self.entry_price) * self.qty
        fees = self.entry_fee + exit_fee
        # Same dict object: markouts still pending keep filling it
        trip = RoundTrip(self.entry_time, self.entry_price, now, price, self.qty,
                         kind, fees, gross - fees, self._marks)
        self.qty = 0.0
        self.round_trips.append(trip)
        self._awaiting.append(trip)
        self._emit_done()

    def _emit_done(self, force: bool = False) -> None:
        """Report closed trips once their markouts are all measured."""
        still = []
        for t in self._awaiting:
            if force or len(t.markouts) == len(MARKOUTS):
                if self._on_round_trip:
                    self._on_round_trip(t)
            else:
                still.append(t)
        self._awaiting = still

    def _check_marks(self, now: float) -> None:
        if math.isnan(self.bid) or math.isnan(self.ask):
            return
        due = [m for m in self._pending_marks if m[0] <= now]
        if not due:
            return
        self._pending_marks = [m for m in self._pending_marks if m[0] > now]
        for _, s, fill_price, marks in due:
            marks[s] = self.mid / fill_price - 1
        self._emit_done()

    def flush(self, now: float) -> None:
        """Shutdown: close any position at the bid and report everything."""
        self.exit_taker(now, "shutdown")
        self._emit_done(force=True)

    def _risk(self, now: float) -> None:
        if self.flat or math.isnan(self.bid):
            return
        kind = None
        if self.mid <= self.entry_price * (1 - self.stop_pct):
            kind = "stop"
        elif now - self.entry_time >= self.max_hold:
            kind = "timeout"
        if kind:
            self.exit_taker(now, kind)

    # -- events --------------------------------------------------------------

    def exit_taker(self, now: float, kind: str) -> None:
        """Market SELL at the best bid (taker fee), cancelling the resting ask."""
        if self.flat or math.isnan(self.bid):
            return
        for o in self.orders:
            o.cancel_at = min(o.cancel_at, now)
        self.orders = []
        price = self.bid
        self._close(now, price, price * self.qty * self.taker_fee, kind)
        self._last_quote = -math.inf

    def on_book(self, now: float, bid: float, bid_size: float, ask: float, ask_size: float) -> None:
        self.bid, self.bid_size, self.ask, self.ask_size = bid, bid_size, ask, ask_size
        self._activate(now)
        self._check_marks(now)
        self._risk(now)
        self._quote(now)

    def on_trade(self, now: float, price: float, qty: float, taker_side: str) -> None:
        self._activate(now)
        for o in self._live(now):
            if o.side == "buy" and taker_side == "sell":
                through = price < o.price
                at = price == o.price
            elif o.side == "sell" and taker_side == "buy":
                through = price > o.price
                at = price == o.price
            else:
                continue
            if at:
                o.consumed += qty
            if through or (at and o.consumed >= o.queue_ahead + o.qty):
                self._fill(o, now)
                break
        self._check_marks(now)
        self._risk(now)
        self._quote(now)


def summarize(trips: list[RoundTrip]) -> dict:
    """Totals over round trips (net of fees)."""
    n = len(trips)
    if not n:
        return {"trades": 0}
    pnls = [t.pnl for t in trips]
    wins = [p for p in pnls if p > 0]
    kinds: dict[str, int] = {}
    for t in trips:
        kinds[t.exit_kind] = kinds.get(t.exit_kind, 0) + 1
    marks = {}
    for s in MARKOUTS:
        vals = [t.markouts[s] for t in trips if s in t.markouts]
        marks[s] = sum(vals) / len(vals) if vals else math.nan
    return {
        "trades": n,
        "pnl": sum(pnls),
        "avg_pnl": sum(pnls) / n,
        "win_rate": len(wins) / n,
        "fees": sum(t.fees for t in trips),
        "exits": kinds,
        "avg_hold_sec": sum(t.exit_time - t.entry_time for t in trips) / n,
        "markouts": marks,
    }
