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

### Backtest em dados recentes

```bash
python run_backtest.py                              # últimos 90 dias, candles de 1m, Binance
python run_backtest.py --days 30
python run_backtest.py --start 2026-07-01 --end 2026-10-01
python run_backtest.py --timeframe 5m --exchange bybit
```

Roda a Strategy V3 com os mesmos parâmetros do robô ao vivo e mostra, além das métricas, o PnL bruto,
as **taxas pagas**, o PnL líquido, o buy & hold do período e um resumo por mês. Os candles baixados
ficam em `backtest_cache/`.

Não usa dados sintéticos: se o download falhar, o script para. A Binance e a Bybit bloqueiam
servidores nos EUA (caso do Codespaces) — **rode do seu computador** (do Brasil a Binance funciona).

### Ajustar a estratégia (menos "medroso") com validação

Os números da estratégia (filtro de volatilidade na entrada, janela do cruzamento, saída por
momentum, stop, trailing, tamanho da posição, timeframe) ficam em `strategy.json`, lido pelo robô
ao vivo, pelo backtest e pela comparação. Sem o arquivo, valem os valores originais da V3.

```bash
python run_backtest.py --optimize --save
```

Testa 72 combinações — das originais até entradas mais soltas e saídas mais folgadas — nos
primeiros 2/3 do período (treino) e confere a melhor nos últimos 1/3 (validação, que ela nunca viu).
Só grava em `strategy.json` se a melhor também der lucro **líquido de taxas** na validação e superar
a atual. Para o robô passar a usar: `sudo systemctl restart trading-bot`. Ao ver parâmetros novos,
ele arquiva a sessão anterior em `archive/` e começa do zero, para a validação ao vivo não misturar
configurações.

### Estratégias disponíveis

| `strategy` | Ideia | Timeframe padrão |
|---|---|---|
| `ma_cross_v3` | cruzamento EMA9 × SMA20 com filtros (original) | 1m |
| `trend_v4` | seguir tendência: compra no rompimento da máxima de N candles (canal de Donchian), sai na perda da mínima de M candles ou no stop móvel; posição dimensionada pelo risco (`risk_pct`, padrão 2% do capital no stop) | 4h |

```bash
python run_backtest.py --strategy trend_v4 --optimize          # 6 anos de 4h: 4 de treino, 2 de validação
python run_backtest.py --strategy trend_v4 --optimize --save   # grava se aprovada
```

A tabela mostra também **comprar e segurar** no mesmo período, como referência.

### Risco por trade (`risk_pct`)

Na `trend_v4` cada entrada é dimensionada para perder `risk_pct` do saldo se o stop inicial for
atingido (padrão `0.02` = 2%; aceita de 0 a 10%), limitada a `max_position_pct` (95%) do saldo.
Mais risco = posições maiores: ganhos **e** quedas crescem juntos — não cria vantagem.

```bash
python run_backtest.py --risk-sweep          # mesma estratégia com 0,5% a 5% por trade, treino e validação
python run_backtest.py --risk 3              # backtest completo com 3%
python run_backtest.py --risk 3 --save       # grava risk_pct=0.03 em strategy.json
```

A coluna `teto` mostra quantas entradas já usaram a posição máxima: nelas, aumentar o risco não
muda nada. Escolha pela maior queda (`dd`) que você aguentaria sem desligar o robô. Mudar o
`risk_pct` arquiva a sessão ao vivo e começa uma nova (`sudo systemctl restart trading-bot`).
Na `ma_cross_v3` (posição fixa, `risk_sizing: false`) o parâmetro não se aplica.

### Operar em segundos? Estudo do "muro das taxas"

Antes de qualquer robô de segundos, `run_seconds_study.py` mede com candles **reais de 1 segundo**
da Binance (arquivo público data.binance.vision, sem chave de API) quanto o BTC anda em 1 s, 5 s,
30 s, 1 min, 15 min, quantas vezes esse movimento cobre a taxa de ida+volta, que taxa de acerto de
direção seria preciso para empatar e quanto acertam os sinais mais simples (momento/reversão).

```bash
python run_seconds_study.py                    # últimos 7 dias (máx. 14), taxas 0,1 / 0,075 / 0,02 / 0% por lado
python run_seconds_study.py --fees 0.1,0       # só essas taxas
```

Não negocia nada. Baixa ~3 MB por dia para `backtest_cache/vision/`. Rode na VM (a Binance
bloqueia servidores nos EUA). O sinal de cada horizonte é escolhido na 1ª metade do período e
medido na 2ª, entrando 1 s atrasado, com margem de ruído estatístico — um "lucro" dentro dela
pode ser sorte. Os dados da Binance Vision são licenciados para estudo/backtest pessoal, não
para gerar ordens ao vivo.

### Robô maker de papel (BTC/FDUSD)

O estudo acima mostra que, com taxa de 0,1%, operar em segundos é impossível. A exceção seria
ordem **maker** (limitada, esperando no livro) em par com taxa maker zero, como o BTC/FDUSD.
`maker_bot.py` testa isso **sem dinheiro e sem chave de API**: escuta o melhor preço e os negócios
em tempo real (websocket) e simula uma ordem de compra no melhor preço de compra e, depois de
executada, uma de venda no melhor preço de venda.

A simulação é conservadora: a ordem entra no fim da fila do preço, só executa depois que essa
fila for consumida (ou se o preço for atravessado), cada ação leva 0,3 s para valer (ida e volta
São Paulo–Tóquio) e uma ordem que cruzaria o livro é recusada (post-only). Saída a mercado, com
taxa taker de 0,1%, se o preço cair 0,2% ou após 5 minutos. Mede também a **seleção adversa**:
para onde o preço vai 1, 5, 30 e 60 s depois de cada compra.

```bash
bash scripts/install_maker.sh                 # serviço maker-bot ao lado do trading-bot
tail -f logs/maker.log                        # fills, idas e voltas, heartbeat a cada 5 min
.venv/bin/python run_maker_report.py          # resumo: resultado, saídas, seleção adversa
sudo systemctl disable --now maker-bot        # desligar
```

Rodam **4 versões ao mesmo tempo, sobre os mesmos dados ao vivo**, para comparar de forma justa:

| versão | o que muda |
|---|---|
| `base` | compra no melhor preço de compra; stop 0,2% ou 5 min, a mercado |
| `filtro` | só compra se a fila de compra ≥ fila de venda e o preço não caiu nos últimos 10 s |
| `saida_maker` | sai com ordem limitada (sem taxa): após 30 s a venda desce até o melhor preço de venda; stop a mercado só em 0,5% |
| `filtro+maker` | as duas mudanças juntas |

Os trades ficam em `maker_trades.csv` (coluna `variant`); um arquivo da versão anterior, sem
essa coluna, é movido para `maker_trades_v1.csv`. Deixe rodar 1–2 semanas antes de concluir. A taxa maker
zero do BTC/FDUSD é promocional ("até segunda ordem"); confira antes de qualquer teste real.

### Rodar comparação Live vs Backtest

```bash
python run_comparison.py
```

O bot grava cada candle fechado (e o warm-up) em `live_bars.csv`. A comparação roda o backtest
(mesma estratégia V3, mesmos parâmetros) **sobre exatamente esses candles** e compara com os trades do
`session.json` — não depende de baixar histórico. Reinícios (ex.: Codespace parado) e reconexões são
reproduzidos como aconteceram: candles de quando o bot estava desligado só aquecem os indicadores.
Uma posição ainda aberta não conta como trade em nenhum dos lados.

Diferença que sobra entre live e backtest: ao vivo os stops são checados a cada poll (5 s); o backtest
só vê a abertura e o fechamento de cada candle.

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

### 3. Prova de vida (a cada 5 min: preço, candles processados, posição, patrimônio, exchange, circuit breaker)

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

## VM 24/7 (Oracle Cloud Always Free, Azure, etc.)

Numa VM Ubuntu, o bot roda como serviço do sistema (sobe no boot, reinicia se cair):

```bash
git clone https://github.com/thiagosaraiv4/trader.git && cd trader
bash scripts/install_vm.sh
```

```bash
sudo systemctl status trading-bot     # está rodando?
tail -f logs/bot.log                  # logs
git pull && sudo systemctl restart trading-bot   # atualizar
```

Na VM use o serviço **ou** o `watchdog.sh`, nunca os dois (`watchdog.sh status` não enxerga o
serviço; use `systemctl status`).

Se o `strategy.json` estiver inválido, o robô **não inicia** (sai com código 2 e o log mostra
`invalid_strategy_json`) em vez de rodar outra estratégia — corrija o arquivo e reinicie.

### Circuit breaker

`halt_drawdown_pct` no `strategy.json`: para de abrir posições quando o patrimônio cai essa fração
abaixo do início da sessão (0 = desligado). Padrão: 7% na `ma_cross_v3`, **desligado na `trend_v4`**
(quedas de 10–20% são normais nela; o risco por trade já é limitado pelo stop). O estado sobrevive a
reinícios e aparece no `heartbeat` (`"halted"`).

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
