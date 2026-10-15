"""
pair_scanner.py
---------------
Scanner de pares para o robô maker: escuta vários pares da Binance ao mesmo
tempo (websocket público, sem chave, sem ordens) e mede, para cada um, se uma
ordem limitada passiva teria vantagem:

    spread      em ticks e em bp (centésimos de 1%)
    execuções   negócios que "executariam" uma ordem nossa no topo do livro:
                alguém vendendo no melhor preço de compra (nossa compra) ou
                comprando no melhor preço de venda (nossa venda)
    markout     quanto o preço médio (mid) andou a nosso favor 1, 5 e 30 s
                depois dessa execução, em bp. Já inclui o spread ganho menos a
                seleção adversa: positivo = a ordem passiva ganhou
        topo        qualquer negócio no preço do topo (otimista: supõe que
                    estávamos na frente da fila)
        atravessou  só negócios que passaram do nível (pessimista: o que uma
                    ordem no fim da fila, como a nossa, consegue)

As estatísticas acumulam em scanner_stats.json (sobrevivem a reinícios);
`python run_scanner_report.py` mostra a tabela.

Uso:
    python pair_scanner.py                          # pares padrão
    python pair_scanner.py --pairs SOL/FDUSD,DOGE/FDUSD
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from collections import Counter, deque
from pathlib import Path

from utils.logger import JsonLineFormatter

ROOT = Path(__file__).resolve().parent
STATS_PATH = ROOT / "scanner_stats.json"
DEFAULT_PAIRS = (
    "BTC/FDUSD", "ETH/FDUSD", "SOL/FDUSD", "XRP/FDUSD", "DOGE/FDUSD",
    "LINK/FDUSD", "BNB/FDUSD", "BTC/BRL", "USDT/BRL",
)
HORIZONS = (1, 5, 30)
KINDS = ("touch", "through")
_SAVE_SEC = 300
_STALE_SEC = 180
# At most one sampled execution per pair/side/kind per this many seconds:
# busy pairs would otherwise flood the markout queue with near-duplicates
_MIN_EVENT_GAP = 0.5
_MAX_GAP_SEC = 60

log = logging.getLogger("scanner")


class PairStats:
    """Running statistics for one pair (JSON-serialisable)."""

    def __init__(self, tick: float) -> None:
        self.tick = tick
        self.seconds = 0                     # one spread sample per second
        self.spread_ticks: Counter[int] = Counter()
        self.spread_bps_sum = 0.0
        self.trades = 0
        self.events: Counter[str] = Counter()          # "touch"/"through"
        # markout sums per kind and horizon: [n, sum, sum of squares] in bp
        self.marks = {k: {h: [0, 0.0, 0.0] for h in HORIZONS} for k in KINDS}

    def to_dict(self) -> dict:
        return {
            "tick": self.tick, "seconds": self.seconds,
            "spread_ticks": {str(k): v for k, v in self.spread_ticks.items()},
            "spread_bps_sum": self.spread_bps_sum, "trades": self.trades,
            "events": dict(self.events),
            "marks": {k: {str(h): v for h, v in hs.items()} for k, hs in self.marks.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> PairStats:
        s = cls(float(d["tick"]))
        s.seconds = int(d.get("seconds", 0))
        s.spread_ticks = Counter({int(k): int(v) for k, v in d.get("spread_ticks", {}).items()})
        s.spread_bps_sum = float(d.get("spread_bps_sum", 0.0))
        s.trades = int(d.get("trades", 0))
        s.events = Counter(d.get("events", {}))
        for k in KINDS:
            for h in HORIZONS:
                v = d.get("marks", {}).get(k, {}).get(str(h))
                if v:
                    s.marks[k][h] = [int(v[0]), float(v[1]), float(v[2])]
        return s


class Scanner:
    def __init__(self, pairs: list[str]) -> None:
        self.pairs = pairs
        self.stats: dict[str, PairStats] = {}
        self.book: dict[str, tuple[float, float]] = {}     # symbol -> (bid, ask)
        # symbol -> (last second sampled, spread ticks, spread bp) at that time
        self.last_sample: dict[str, tuple[int, int, float]] = {}
        self.last_event: dict[tuple[str, str, str], float] = {}
        # symbol -> horizon -> deque of (due, kind, side, price), in due order
        self.pending: dict[str, dict[int, deque]] = {}
        self.stop = asyncio.Event()
        self.last_data = time.time()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        if not STATS_PATH.exists():
            return
        try:
            data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            for sym, d in data.get("pairs", {}).items():
                self.stats[sym] = PairStats.from_dict(d)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("stats_unreadable — starting fresh", extra={"error": str(e)})

    def save(self) -> None:
        data = {"updated": time.time(), "pairs": {s: p.to_dict() for s, p in self.stats.items()}}
        tmp = STATS_PATH.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, STATS_PATH)
        except OSError as e:
            log.error("stats_write_failed", extra={"error": str(e)})

    # -- events ---------------------------------------------------------------

    def on_book(self, now: float, sym: str, bid: float, ask: float) -> None:
        if not (bid > 0 and ask > bid):
            return
        self.book[sym] = (bid, ask)
        st = self.stats[sym]
        mid = (bid + ask) / 2
        sec = int(now)
        ticks = max(1, round((ask - bid) / st.tick))
        bps = (ask - bid) / mid * 1e4
        prev = self.last_sample.get(sym)
        if prev is None:
            self.last_sample[sym] = (sec, ticks, bps)
        elif prev[0] != sec:
            # Time-weighted: the previous spread held for every second since
            # it was seen (a quiet pair updates its book rarely). Gaps longer
            # than _MAX_GAP_SEC are a disconnect, not a quiet book.
            held = min(sec - prev[0], _MAX_GAP_SEC)
            st.seconds += held
            st.spread_ticks[prev[1]] += held
            st.spread_bps_sum += prev[2] * held
            self.last_sample[sym] = (sec, ticks, bps)
        else:
            # Several updates in the same second: keep the latest
            self.last_sample[sym] = (sec, ticks, bps)
        for h, q in self.pending[sym].items():
            while q and q[0][0] <= now:
                _, kind, side, price = q.popleft()
                gain = (mid - price) / price if side == "buy" else (price - mid) / price
                m = st.marks[kind][h]
                bp = gain * 1e4
                m[0] += 1
                m[1] += bp
                m[2] += bp * bp

    def on_trade(self, now: float, sym: str, price: float, taker_side: str) -> None:
        book = self.book.get(sym)
        st = self.stats[sym]
        st.trades += 1
        if not book:
            return
        bid, ask = book
        # A taker SELL hits bids: our passive BUY would fill (and vice versa)
        if taker_side == "sell" and price <= bid:
            side, kind = "buy", ("through" if price < bid else "touch")
        elif taker_side == "buy" and price >= ask:
            side, kind = "sell", ("through" if price > ask else "touch")
        else:
            return
        kinds = ("touch", "through") if kind == "through" else ("touch",)
        for k in kinds:
            key = (sym, side, k)
            if now - self.last_event.get(key, 0.0) < _MIN_EVENT_GAP:
                continue
            self.last_event[key] = now
            st.events[k] += 1
            # Fill at our resting price: the touch we were quoting
            fill = bid if side == "buy" else ask
            for h in HORIZONS:
                self.pending[sym][h].append((now + h, k, side, fill))

    # -- network ----------------------------------------------------------------

    async def _exchange(self):
        import ccxt
        import ccxt.pro as ccxtpro
        ex = ccxtpro.binance({"enableRateLimit": True, "newUpdates": True,
                              "options": {"defaultType": "spot",
                                          "fetchMarkets": {"types": ["spot"]}}})
        ids = [p.replace("/", "") for p in self.pairs]
        try:
            # Only our symbols: the full market list costs ~100 MB of RAM
            markets = await ex.load_markets(False, {"symbols": json.dumps(ids, separators=(",", ":"))})
        except ccxt.BadSymbol as e:
            await ex.close()
            raise SystemExit(f"par inválido na lista: {e}")
        missing = [p for p in self.pairs if p not in markets]
        if missing:
            log.warning("pairs_not_found — ignorados", extra={"pairs": missing})
        self.pairs = [p for p in self.pairs if p in markets]
        if not self.pairs:
            await ex.close()
            raise SystemExit("nenhum par disponível")
        for p in self.pairs:
            tick = float(markets[p]["precision"]["price"] or 0.01)
            if p not in self.stats:
                self.stats[p] = PairStats(tick)
            self.stats[p].tick = tick
            self.pending.setdefault(p, {h: deque() for h in HORIZONS})
        return ex

    async def _books(self, ex) -> None:
        while not self.stop.is_set():
            data = await ex.watch_bids_asks(self.pairs)
            now = time.time()
            self.last_data = now
            for sym, t in data.items():
                if sym in self.stats and t.get("bid") is not None and t.get("ask") is not None:
                    self.on_book(now, sym, float(t["bid"]), float(t["ask"]))

    async def _trades(self, ex) -> None:
        while not self.stop.is_set():
            trades = await ex.watch_trades_for_symbols(self.pairs)
            now = time.time()
            self.last_data = now
            for tr in trades:
                sym = tr.get("symbol")
                if sym in self.stats and tr.get("side") in ("buy", "sell"):
                    self.on_trade(now, sym, float(tr["price"]), tr["side"])

    async def _housekeeping(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=_SAVE_SEC)
            except asyncio.TimeoutError:
                pass
            self.save()
            summary = {}
            for sym, st in self.stats.items():
                n, s, _ = st.marks["touch"][5]
                summary[sym] = {"hours": round(st.seconds / 3600, 1),
                                "spread_bps": round(st.spread_bps_sum / st.seconds, 2) if st.seconds else None,
                                "markout5_bps": round(s / n, 2) if n else None}
            log.info("heartbeat", extra={"pairs": summary})
            if time.time() - self.last_data > _STALE_SEC:
                raise ConnectionError(f"no market data for {_STALE_SEC}s")

    async def run(self) -> None:
        self.load()
        backoff = 5
        while not self.stop.is_set():
            ex = None
            connected_at = time.time()
            try:
                ex = await self._exchange()
                self.last_data = time.time()
                log.info("scanner_started", extra={"pairs": self.pairs})
                tasks = [asyncio.create_task(c) for c in
                         (self._books(ex), self._trades(ex), self._housekeeping())]
                stop_task = asyncio.create_task(self.stop.wait())
                done, _ = await asyncio.wait(tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED)
                for t in tasks + [stop_task]:
                    t.cancel()
                for t in done:
                    if t is not stop_task and t.exception():
                        raise t.exception()
                backoff = 5
            except SystemExit:
                raise
            except Exception as e:
                # A connection that worked for a while starts the backoff over:
                # Binance drops long-lived streams now and then, and a backoff
                # that only grows would leave the stream down minutes at a time
                if time.time() - connected_at > 60:
                    backoff = 5
                log.warning("stream_error — reconnecting", extra={"error": str(e), "backoff_sec": backoff})
                # Pending markouts span the gap: drop them rather than score stale prices
                for qs in self.pending.values():
                    for q in qs.values():
                        q.clear()
                self.book.clear()
                self.last_sample.clear()
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 300)
            finally:
                if ex is not None:
                    try:
                        await ex.close()
                    except Exception:
                        pass
        self.save()
        log.info("scanner_stopped")


def main() -> None:
    p = argparse.ArgumentParser(description="Scanner de pares para o robô maker (Binance).")
    p.add_argument("--pairs", default=",".join(DEFAULT_PAIRS),
                   help="pares separados por vírgula (padrão: FDUSD e BRL principais)")
    args = p.parse_args()
    pairs = [x.strip().upper() for x in args.pairs.split(",") if x.strip()]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    root.addHandler(handler)
    scanner = Scanner(pairs)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, scanner.stop.set)
        await scanner.run()

    try:
        asyncio.run(_main())
    except SystemExit as e:
        if isinstance(e.code, str):
            log.error("scanner_not_started", extra={"error": e.code})
            sys.exit(2)
        raise


if __name__ == "__main__":
    main()
