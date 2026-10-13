"""
maker_bot.py
------------
Robô MAKER de papel: escuta o melhor preço de compra/venda (bookTicker) e os
negócios do BTC/FDUSD na Binance em tempo real (websocket) e simula ordens
limitadas passivas com core/maker_sim.py. Não envia ordens nem usa chave de
API — só mede se a ideia daria dinheiro.

Cada ida e volta (compra executada → venda) vai para maker_trades.csv;
`python run_maker_report.py` resume. Prova de vida a cada 5 min no log.

Uso (na VM, ao lado do trading-bot):
    python maker_bot.py                          # BTC/FDUSD, 1000 FDUSD por ordem
    python maker_bot.py --maker-fee 0 --taker-fee 0.1 --latency 0.3

Serviço: bash scripts/install_maker.sh
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from core.maker_sim import MARKOUTS, MakerSim, RoundTrip, summarize
from utils.logger import JsonLineFormatter

ROOT = Path(__file__).resolve().parent
TRADES_PATH = ROOT / "maker_trades.csv"
_FIELDS = ("entry_time", "exit_time", "entry_price", "exit_price", "qty", "exit_kind",
           "fees", "pnl") + tuple(f"m{s}" for s in MARKOUTS)
_HEARTBEAT_SEC = 300
# A stream silent for this long is treated as dead (BTC/FDUSD trades every
# few seconds; the bookTicker even more often)
_STALE_SEC = 120

log = logging.getLogger("maker")


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    root.addHandler(handler)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def load_trips(path: Path = TRADES_PATH) -> list[RoundTrip]:
    """Round trips recorded so far (malformed rows are skipped)."""
    trips: list[RoundTrip] = []
    if not path.exists():
        return trips
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                marks = {s: float(row[f"m{s}"]) for s in MARKOUTS if row.get(f"m{s}")}
                trips.append(RoundTrip(
                    datetime.fromisoformat(row["entry_time"]).timestamp(),
                    float(row["entry_price"]),
                    datetime.fromisoformat(row["exit_time"]).timestamp(),
                    float(row["exit_price"]), float(row["qty"]), row["exit_kind"],
                    float(row["fees"]), float(row["pnl"]), marks,
                ))
            except (KeyError, ValueError, TypeError):
                continue
    return trips


def _append_trip(trip: RoundTrip, path: Path = TRADES_PATH) -> None:
    new = not path.exists()
    try:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(_FIELDS)
            w.writerow([_iso(trip.entry_time), _iso(trip.exit_time), trip.entry_price,
                        trip.exit_price, trip.qty, trip.exit_kind, round(trip.fees, 6),
                        round(trip.pnl, 6)]
                       + [f"{trip.markouts[s]:.8f}" if s in trip.markouts else "" for s in MARKOUTS])
    except OSError as e:
        log.error("trade_write_failed", extra={"error": str(e)})


def _acquire_lock() -> TextIO | None:
    import fcntl
    fh = open(ROOT / "maker.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


class MakerBot:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.symbol = args.symbol
        self.stop = asyncio.Event()
        self.history = load_trips()
        self.sim: MakerSim | None = None
        self.last_book = self.last_trade = time.time()
        self.events = 0

    def _on_trip(self, trip: RoundTrip) -> None:
        self.history.append(trip)
        _append_trip(trip)
        log.info("round_trip", extra={
            "entry": trip.entry_price, "exit": trip.exit_price, "kind": trip.exit_kind,
            "pnl": round(trip.pnl, 4), "hold_sec": round(trip.exit_time - trip.entry_time, 1),
            "markouts_pct": {s: round(v * 100, 4) for s, v in trip.markouts.items()},
        })

    def _on_fill(self, side: str, price: float, qty: float, now: float) -> None:
        log.info("fill", extra={"side": side, "price": price, "qty": qty})

    async def _exchange(self):
        import ccxt.pro as ccxtpro
        import ccxt
        # Ask exchangeInfo for our symbol only: parsing the full Binance market
        # list costs ~100 MB that Python never hands back (1 GB VM)
        ex = ccxtpro.binance({"enableRateLimit": True, "newUpdates": True,
                              "options": {"defaultType": "spot",
                                          "fetchMarkets": {"types": ["spot"]}}})
        try:
            markets = await ex.load_markets(False, {"symbol": self.symbol.replace("/", "")})
        except ccxt.BadSymbol:
            markets = {}
        m = markets.get(self.symbol)
        if m is None or not m.get("active", True):
            await ex.close()
            raise SystemExit(f"{self.symbol} não está disponível na Binance")
        tick = float(m["precision"]["price"] or 0.01)
        return ex, tick

    async def _books(self, ex) -> None:
        while not self.stop.is_set():
            data = await ex.watch_bids_asks([self.symbol])
            t = data.get(self.symbol)
            if not t or t.get("bid") is None or t.get("ask") is None:
                continue
            now = time.time()
            self.last_book = now
            self.events += 1
            self.sim.on_book(now, float(t["bid"]), float(t.get("bidVolume") or 0.0),
                             float(t["ask"]), float(t.get("askVolume") or 0.0))

    async def _trades(self, ex) -> None:
        last_id = None
        while not self.stop.is_set():
            trades = await ex.watch_trades(self.symbol)
            now = time.time()
            self.last_trade = now
            for tr in trades:
                try:
                    tid = int(tr["id"])
                except (KeyError, TypeError, ValueError):
                    tid = None
                if tid is not None:
                    if last_id is not None and tid <= last_id:
                        continue    # already seen (cache replay)
                    last_id = tid
                if tr.get("side") not in ("buy", "sell"):
                    continue
                self.events += 1
                self.sim.on_trade(now, float(tr["price"]), float(tr["amount"]), tr["side"])

    async def _heartbeat(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=_HEARTBEAT_SEC)
            except asyncio.TimeoutError:
                pass
            s = summarize(self.history)
            sim = self.sim
            log.info("heartbeat", extra={
                "symbol": self.symbol, "events": self.events,
                "bid": sim.bid if sim else None, "ask": sim.ask if sim else None,
                "position": "LONG" if sim and not sim.flat else "FLAT",
                "orders_placed": sim.orders_placed if sim else 0,
                "trades": s["trades"], "pnl": round(s.get("pnl", 0.0), 4),
                "win_rate": round(s.get("win_rate", 0.0), 3),
            })

    async def _watchdog(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(15)
            idle = time.time() - min(self.last_book, self.last_trade)
            if idle > _STALE_SEC:
                raise ConnectionError(f"no market data for {idle:.0f}s")

    async def run(self) -> None:
        a = self.args
        backoff = 5
        while not self.stop.is_set():
            ex = None
            try:
                ex, tick = await self._exchange()
                # A reconnect starts flat: a position held through a data gap
                # would be valued on prices nobody saw
                if self.sim is not None:
                    self.sim.flush(time.time())
                self.sim = MakerSim(
                    notional=a.notional, maker_fee=a.maker_fee / 100, taker_fee=a.taker_fee / 100,
                    tick=tick, latency_sec=a.latency, stop_pct=a.stop / 100,
                    max_hold_sec=a.max_hold, on_round_trip=self._on_trip, on_fill=self._on_fill,
                )
                self.last_book = self.last_trade = time.time()
                log.info("maker_started", extra={
                    "symbol": self.symbol, "tick": tick, "notional": a.notional,
                    "maker_fee_pct": a.maker_fee, "taker_fee_pct": a.taker_fee,
                    "latency_sec": a.latency, "stop_pct": a.stop, "max_hold_sec": a.max_hold,
                    "trades": len(self.history),
                })
                tasks = [asyncio.create_task(c) for c in
                         (self._books(ex), self._trades(ex), self._watchdog(),
                          self._heartbeat())]
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
            except Exception as e:  # network, exchange, stale stream
                log.warning("stream_error — reconnecting", extra={"error": str(e), "backoff_sec": backoff})
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
        if self.sim is not None:
            self.sim.flush(time.time())
        log.info("maker_stopped", extra={"trades": len(self.history)})


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Robô maker de papel (BTC/FDUSD).")
    p.add_argument("--symbol", default="BTC/FDUSD")
    p.add_argument("--notional", type=float, default=1000.0, help="valor por ordem (padrão 1000)")
    p.add_argument("--maker-fee", type=float, default=0.0, help="%% por lado (padrão 0)")
    p.add_argument("--taker-fee", type=float, default=0.1, help="%% por lado nas saídas forçadas (padrão 0.1)")
    p.add_argument("--latency", type=float, default=0.3, help="segundos até a ordem valer (padrão 0.3)")
    p.add_argument("--stop", type=float, default=0.2, help="%% abaixo da entrada para sair a mercado (padrão 0.2)")
    p.add_argument("--max-hold", type=float, default=300.0, help="segundos máximos posicionado (padrão 300)")
    args = p.parse_args(argv)
    for name in ("notional", "latency", "stop", "max_hold"):
        if getattr(args, name) <= 0:
            p.error(f"--{name.replace('_', '-')} precisa ser > 0")
    if not (0 <= args.maker_fee < 1 and 0 <= args.taker_fee < 1):
        p.error("taxas em %% por lado, entre 0 e 1")
    return args


def main() -> None:
    args = _parse_args()
    _setup_logging()
    lock = _acquire_lock()
    if lock is None:
        log.error("another_instance_running — exiting")
        sys.exit(0)
    bot = MakerBot(args)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop.set)
        await bot.run()

    try:
        asyncio.run(_main())
    except SystemExit as e:
        if isinstance(e.code, str):
            log.error("maker_not_started", extra={"error": e.code})
            sys.exit(2)
        raise


if __name__ == "__main__":
    main()
