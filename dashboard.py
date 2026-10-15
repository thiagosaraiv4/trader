"""
dashboard.py
------------
Painel web dos robôs: gráfico de candles com entradas/saídas, patrimônio,
trades, status dos serviços, relatórios do scanner e do maker e logs. Só
leitura — não liga, desliga nem altera nada.

Uso:
    python dashboard.py                    # http://127.0.0.1:8080 (só na própria máquina)
    python dashboard.py --port 8090

Acesso de fora (celular, outro PC): o padrão escuta só em 127.0.0.1. O jeito
seguro é um túnel SSH a partir do seu computador:
    ssh -i CHAVE.key -L 8080:127.0.0.1:8080 ubuntu@IP_DA_VM
e abrir http://localhost:8080 no navegador.

Para escutar em todas as interfaces (--host 0.0.0.0) é obrigatório definir
uma senha — em TRADER_PANEL_PASSWORD ou no arquivo .panel_password (fora do
git) — e abrir a porta no firewall da nuvem. Sem HTTPS a senha trafega em
texto: prefira o túnel SSH.
"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path

from aiohttp import web

from web.server import create_app

ROOT = Path(__file__).resolve().parent
PASSWORD_FILE = ROOT / ".panel_password"
_MIN_PASSWORD = 10


def _password() -> str | None:
    pw = os.environ.get("TRADER_PANEL_PASSWORD")
    if not pw and PASSWORD_FILE.exists():
        pw = PASSWORD_FILE.read_text(encoding="utf-8").strip()
    return pw or None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="Painel web dos robôs (só leitura).")
    p.add_argument("--host", default="127.0.0.1", help="padrão: 127.0.0.1 (só esta máquina)")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    password = _password()
    if not _is_loopback(args.host):
        if not password:
            p.error(f"--host {args.host} expõe o painel na rede: defina uma senha em "
                    f"TRADER_PANEL_PASSWORD ou {PASSWORD_FILE.name}")
        if len(password) < _MIN_PASSWORD:
            p.error(f"senha curta demais (mínimo {_MIN_PASSWORD} caracteres)")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    print(f"Painel em http://{args.host}:{args.port}  (login {'ativo' if password else 'desativado'})",
          flush=True)
    web.run_app(create_app(password), host=args.host, port=args.port, print=None,
                access_log=None)


if __name__ == "__main__":
    main()
