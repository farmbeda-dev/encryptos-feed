#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENCRYPTOS -- fetch_eassets_github.py

Busca o arquivo eassets mais recente no repo GitHub, mesmo com nome
variavel (data/hora no nome). Usa a API do GitHub pra listar e achar
o mais novo por ordem alfabetica (funciona porque o formato
eassets-panel-YYYYMMDD-HHMMSS.json ordena cronologicamente por nome).
"""

import json
import urllib.request
import urllib.error

REPO = "farmbeda-dev/eassets-drop"
API_URL = f"https://api.github.com/repos/{REPO}/contents/"
OUT_FILE = "/opt/encryptos/input/eassets-latest.json"


def log(msg):
    print(msg)


def main():
    req = urllib.request.Request(API_URL, headers={"User-Agent": "encryptos-fetch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            files = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"ERRO ao listar repo: HTTP {e.code}")
        return

    candidatos = [
        f for f in files
        if f["name"].startswith("eassets-panel-") and f["name"].endswith(".json")
    ]
    if not candidatos:
        log("Nenhum arquivo eassets-panel-*.json encontrado no repo.")
        return

    candidatos.sort(key=lambda f: f["name"], reverse=True)
    mais_recente = candidatos[0]
    log(f"Mais recente encontrado: {mais_recente['name']}")

    download_url = mais_recente["download_url"]
    req2 = urllib.request.Request(download_url, headers={"User-Agent": "encryptos-fetch/1.0"})
    with urllib.request.urlopen(req2, timeout=20) as r:
        conteudo = r.read()

    with open(OUT_FILE, "wb") as f:
        f.write(conteudo)

    log(f"Salvo em {OUT_FILE} ({len(conteudo)} bytes)")


if __name__ == "__main__":
    main()
