"""Vigilante de reestock de Gymshark para la nube (GitHub Actions).

Mira los productos de productos.txt cada ~30 s y manda un aviso al movil
(ntfy) cuando una talla vuelve a tener stock. Cada ejecucion dura unas
5 h 40 min; al acabar, el propio flujo lanza la siguiente.
El canal de ntfy se lee del secreto NTFY_TOPIC (nunca va en el codigo).
"""
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

CARPETA = Path(__file__).parent
ARCHIVO_PRODUCTOS = CARPETA / "productos.txt"
ARCHIVO_ESTADO = CARPETA / "estado.json"    # se guarda en cache entre ejecuciones
CANAL = os.environ.get("NTFY_TOPIC", "")
DURACION = int(os.environ.get("DURACION_SEGUNDOS", 5 * 3600 + 40 * 60))
INTERVALO = 25
MADRID = ZoneInfo("Europe/Madrid")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def ahora():
    return datetime.now(MADRID)


def log(msg):
    print(f"[{ahora():%H:%M:%S}] {msg}", flush=True)


def es_de_noche():
    return ahora().hour >= 23 or ahora().hour < 9


def leer_productos():
    productos = []
    for linea in ARCHIVO_PRODUCTOS.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        url, _, tallas = linea.partition("|")
        url = url.strip().split("?")[0]
        partes = urlparse(url)
        if partes.scheme != "https" or not (partes.netloc == "gymshark.com"
                                            or partes.netloc.endswith(".gymshark.com")):
            log(f"Ignoro una linea que no es de gymshark.com: {url}")
            continue
        tallas = {t.strip().lower() for t in tallas.split(",") if t.strip()}
        productos.append((url, tallas))
    return productos


def consultar(sesion, url):
    r = sesion.get(url, timeout=20)
    r.raise_for_status()
    m = NEXT_DATA.search(r.text)
    if not m:
        raise ValueError("la pagina no trae datos de producto")
    p = json.loads(m.group(1))["props"]["pageProps"]["productData"]["product"]
    stock = {v["size"].lower(): bool(v["inStock"]) for v in p["availableSizes"]}
    variantes = {v["size"].lower(): v["id"] for v in p["availableSizes"]}
    return f'{p["title"]} ({p["colour"]})', stock, variantes


def enviar(titulo, texto, url, prioridad, etiqueta):
    if not CANAL:
        log("Falta el secreto NTFY_TOPIC: no puedo avisar al movil.")
        return
    try:
        requests.post("https://ntfy.sh/", timeout=10, json={
            "topic": CANAL, "title": titulo, "message": texto, "click": url,
            "priority": prioridad, "tags": [etiqueta],
        }).raise_for_status()
    except Exception as e:
        log(f"No se pudo avisar al movil: {e}")


class Avisos:
    """Avisos urgentes repetidos sin frenar la vigilancia."""

    def __init__(self):
        self.pendientes = []   # [momento, titulo, texto, url, numero, total, pausa]

    def reestock(self, nombre, tallas, url):
        total, pausa = (10, 45) if es_de_noche() else (5, 60)
        texto = "Hay stock en: " + ", ".join(t.upper() for t in tallas)
        log(f"*** REESTOCK *** {nombre} -> {texto}")
        self.pendientes.append([time.time(), f"¡Reestock! {nombre}", texto, url, 1, total, pausa])
        self.procesar()

    def procesar(self):
        for a in list(self.pendientes):
            momento, titulo, texto, url, n, total, pausa = a
            if time.time() < momento:
                continue
            extra = f" (aviso {n}/{total})" if n > 1 else ""
            enviar(titulo, texto + " - Pulsa para pagar" + extra, url, 5, "rotating_light")
            if n >= total:
                self.pendientes.remove(a)
            else:
                a[0], a[4] = time.time() + pausa, n + 1


def main():
    sesion = requests.Session()
    sesion.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    try:
        anterior = json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        log(f"Estado anterior recuperado ({len(anterior)} productos).")
    except Exception:
        anterior = {}
    avisos = Avisos()
    fin = time.time() + DURACION
    errores_seguidos = 0
    log("Vigilante en la nube en marcha.")

    while time.time() < fin:
        for url, vigiladas in leer_productos():
            try:
                nombre, stock, variantes = consultar(sesion, url)
                errores_seguidos = 0
            except Exception as e:
                errores_seguidos += 1
                log(f"Error con {url}: {e}")
                if errores_seguidos == 20:
                    enviar("Vigilante Gymshark: problema",
                           f"Gymshark esta rechazando las consultas desde la nube ({e}).",
                           "https://eu.gymshark.com/", 3, "warning")
                continue
            tallas = vigiladas or set(stock)
            con_stock = sorted(t for t in tallas if stock.get(t))
            if url not in anterior:
                log(f"{nombre}: con stock {con_stock or '-'}")
            else:
                nuevas = [t for t in con_stock if not anterior[url].get(t)]
                if nuevas:
                    avisos.reestock(nombre, nuevas,
                                    f"https://eu.checkout.gymshark.com/cart/{variantes[nuevas[0]]}:1")
                agotadas = [t for t in tallas if anterior[url].get(t) and not stock.get(t)]
                if agotadas:
                    log(f"--- AGOTADO --- {nombre} -> {agotadas}")
                    enviar(f"Se ha agotado: {nombre}",
                           "Ya no queda: " + ", ".join(t.upper() for t in agotadas), url, 3, "x")
            anterior[url] = stock
            ARCHIVO_ESTADO.write_text(json.dumps(anterior), encoding="utf-8")
            avisos.procesar()
            time.sleep(1.5)
        espera = time.time() + INTERVALO + random.uniform(0, 8)
        while time.time() < espera:
            avisos.procesar()
            time.sleep(2)

    while avisos.pendientes:     # termina de mandar los avisos repetidos
        avisos.procesar()
        time.sleep(2)
    log("Fin de esta ejecucion; arranca la siguiente.")


if __name__ == "__main__":
    main()
