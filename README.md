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

### Iniciar em background

```bash
nohup python headless_main.py > bot.log 2>&1 &
echo $! > bot.pid
```

### Monitorar

```bash
tail -f bot.log | python -m json.tool
```

### Encerrar gracefully

```bash
kill -SIGTERM $(cat bot.pid)
```

O bot salva `session.json` automaticamente antes de encerrar.

### Rodar comparação Live vs Backtest

```bash
python run_comparison.py
```

---

## Monitoramento Diário

### 1. Verificar se o bot está rodando

```bash
ps aux | grep headless_main
```

### 2. Últimas 20 linhas do log

```bash
tail -20 bot.log | python -m json.tool
```

### 3. Verificar trades do dia

```bash
grep '"trade_close"' bot.log | tail -5
```

### 4. Verificar performance acumulada

```bash
grep '"performance_snapshot"' bot.log | tail -1 | python -m json.tool
```

---

## GitHub Codespaces

O repositório inclui `.devcontainer/devcontainer.json` pré-configurado:

- **Imagem**: Python 3.11
- **Dependências**: Instaladas automaticamente via `requirements.txt`
- **Variável**: `TRADING_LOG_DIR=/workspaces/pasta-gemini-trade/logs`

Ao abrir no Codespaces, o ambiente está pronto para executar:

```bash
python headless_main.py
```

---

## Persistência

O arquivo `session.json` armazena o estado financeiro da sessão:

- Balance atual
- Posição aberta (se houver)
- Histórico completo de trades

**⚠️ `session.json` está no `.gitignore` — nunca é commitado.**

Salvamento automático:
- A cada 3600 segundos (1 hora)
- No shutdown graceful (SIGTERM/SIGINT)

---

## Stack Técnica

| Componente | Tecnologia |
|---|---|
| Dados de mercado | ccxt (Binance público) |
| Indicadores | NumPy (O(1) incremental) |
| Estratégia | TrendFilteredMACross V2/V3 |
| Execução | Paper Trading (0.1% comissão) |
| UI (Desktop) | PySide6 + pyqtgraph |
| Event Loop (Desktop) | qasync |
| Event Loop (Headless) | asyncio puro |
| Logging | JSON estruturado |
