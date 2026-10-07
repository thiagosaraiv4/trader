# Trading Platform — V3

Plataforma de trading algorítmico com paper trading, indicadores técnicos incrementais (O(1)), e arquitetura hexagonal (Ports & Adapters).

Suporta dois modos de execução: **Desktop (UI)** e **Headless (Cloud/Codespaces)**.

---

## Arquitetura

```
                    ┌──────────────────────┐
                    │   DataFeedManager    │
                    │   (Binance / ccxt)   │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   IndicatorEngine    │
                    │  SMA 20/200, EMA 9  │
                    │      ATR 14         │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │     AlgoEngine       │
                    │  Strategy + Risk     │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  PaperTradingCore    │
                    │  (Pure Business Logic)│
                    └──────────┬───────────┘
                          ┌────┴────┐
                   ┌──────▼──┐  ┌───▼──────────┐
                   │ Qt Adpt │  │ Headless Adpt│
                   │(Desktop)│  │  (Cloud/JSON)│
                   └─────────┘  └──────────────┘
```

---

## Modo Desktop (UI)

Requer: `PySide6`, `pyqtgraph`, `qasync`

### Instalação

```bash
pip install -r requirements-ui.txt
```

### Execução

```bash
python main.py
```

Interface gráfica com chart de candlesticks, painel de posições, boleta de ordens, e analytics de performance em tempo real.

---

## Modo Headless (Cloud / Codespaces)

Requer: `numpy`, `pandas`, `ccxt` (sem Qt)

### Instalação

```bash
pip install -r requirements.txt
```

### Iniciar / parar / status (watchdog)

O `scripts/watchdog.sh` sobe o bot, reinicia em caso de crash e faz o shutdown graceful
(o bot salva `session.json` ao receber SIGTERM).

```bash
setsid nohup bash scripts/watchdog.sh start > /dev/null 2>&1 &   # iniciar em background
bash scripts/watchdog.sh status                                 # bot + watchdog rodando?
bash scripts/watchdog.sh logs                                   # tail -f logs/bot.log
bash scripts/watchdog.sh stop                                   # encerrar gracefully
```

Só pode existir **um** bot por pasta: uma segunda instância encontra `session.lock` ocupado e sai
sozinha (evita dois processos gravando o mesmo `session.json`).

Para rodar em primeiro plano (debug), pare o watchdog antes e use `python headless_main.py`.

### Rodar comparação Live vs Backtest

```bash
python run_comparison.py
```

O bot grava cada candle fechado (e o warm-up) em `live_bars.csv`. A comparação roda o backtest
(mesma estratégia V3, mesmos parâmetros) **sobre exatamente esses candles** e compara com os trades do
`session.json` — não depende de baixar histórico.

Sem `live_bars.csv`, o backtest tenta baixar da Binance; de servidores nos EUA (caso comum no
Codespaces) ela bloqueia o acesso e a comparação é abortada em vez de usar dados sintéticos.

Para começar uma comparação limpa (ex.: depois de trocar a estratégia), mova `session.json` e
`live_bars.csv` para outra pasta com o bot parado.

---

## Monitoramento Diário

Logs em `logs/bot.log` (uma linha JSON por evento) e `logs/watchdog.log`.

### 1. Verificar se o bot está rodando

```bash
bash scripts/watchdog.sh status
```

### 2. Últimas 20 linhas do log

```bash
tail -20 logs/bot.log | python -m json.tool --json-lines
```

### 3. Prova de vida (a cada 5 min: preço atual, candles processados, posição)

```bash
grep '"heartbeat"' logs/bot.log | tail -1 | python -m json.tool
```

### 4. Verificar trades do dia

```bash
grep '"trade_close"' logs/bot.log | tail -5
```

### 5. Verificar performance acumulada

```bash
grep '"performance_snapshot"' logs/bot.log | tail -1 | python -m json.tool
```

---

## GitHub Codespaces

O repositório inclui `.devcontainer/devcontainer.json`:

- **Imagem**: Python 3.11
- **Dependências**: instaladas automaticamente via `requirements.txt`
- **Auto-start**: o watchdog (e o bot) sobem sozinhos toda vez que o Codespace inicia —
  não é preciso rodar nada. Confira com `bash scripts/watchdog.sh status`.

### Limites do Codespaces (importante)

O Codespaces **não roda 24/7**:

- **Idle timeout**: o Codespace para depois de um tempo sem interação (padrão 30 min, máximo
  240 min). Configure em <https://github.com/settings/codespaces> → *Default idle timeout*.
  Processos em background não contam como atividade.
- **Cota gratuita**: 120 core-horas/mês na conta pessoal Free (≈ 60 h numa máquina de 2 cores).
- **Região**: de servidores nos EUA a Binance e a Bybit bloqueiam o acesso; o bot cai
  automaticamente para a Kraken (`Kraken exchange initialized (active)` no log).

Quando o Codespace para, o estado fica salvo em `session.json` (a cada 5 min e no shutdown) e o
bot retoma ao reabrir. Para rodar 24/7 de verdade, use uma VPS/VM e o mesmo `watchdog.sh`.

---

## Persistência

O arquivo `session.json` armazena o estado financeiro da sessão:

- Balance atual
- Posição aberta (se houver)
- Histórico completo de trades

O arquivo `live_bars.csv` guarda os candles que o bot usou (para `run_comparison.py`).

**⚠️ `session.json` e `live_bars.csv` estão no `.gitignore` — nunca são commitados.**

Salvamento automático:
- A cada 300 segundos (5 min)
- No shutdown graceful (SIGTERM/SIGINT)

---

## Stack Técnica

| Componente | Tecnologia |
|---|---|
| Dados de mercado | ccxt (Binance público) |
| Indicadores | NumPy (O(1) incremental) |
| Estratégia | TrendFilteredMACross V3 (live e backtest) |
| Execução | Paper Trading (0.1% comissão) |
| UI (Desktop) | PySide6 + pyqtgraph |
| Event Loop (Desktop) | qasync |
| Event Loop (Headless) | asyncio puro |
| Logging | JSON estruturado |
