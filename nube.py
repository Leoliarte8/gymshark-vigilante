"""Vigilante de reestock de Gymshark para la nube (GitHub Actions).

Mira los productos de productos.txt cada ~30 s. Cuando TODOS los productos
obligatorios tienen stock a la vez, manda al movil (ntfy) un aviso urgente
y repetido con un enlace que abre el pago con todo en una sola cesta
(obligatorios + opcionales con stock) y el codigo de descuento puesto.

Secretos (nunca van en el codigo):
  NTFY_TOPIC            canal de ntfy
  CODIGO_DESCUENTO      codigo principal (el de estudiante)
  CODIGO_RESPALDO       codigo por si el principal falla
"""
import html
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import requests

CARPETA = Path(__file__).parent
ARCHIVO_PRODUCTOS = CARPETA / "productos.txt"
ARCHIVO_ESTADO = CARPETA / "estado.json"    # se guarda en cache entre ejecuciones
CANAL = os.environ.get("NTFY_TOPIC", "")
CODIGO = os.environ.get("CODIGO_DESCUENTO", "").strip()
RESPALDO = os.environ.get("CODIGO_RESPALDO", "").strip()
DURACION = int(os.environ.get("DURACION_SEGUNDOS", 5 * 3600 + 40 * 60))
INTERVALO = 25
MADRID = ZoneInfo("Europe/Madrid")
PAGO = "https://eu.checkout.gymshark.com/cart/"

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
    """Devuelve una lista de (url, talla, obligatorio)."""
    articulos = []
    for linea in ARCHIVO_PRODUCTOS.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        partes = [p.strip() for p in linea.split("|")]
        url = partes[0].split("?")[0]
        dominio = urlparse(url)
        if dominio.scheme != "https" or not (dominio.netloc == "gymshark.com"
                                             or dominio.netloc.endswith(".gymshark.com")):
            log(f"Ignoro una linea que no es de gymshark.com: {url}")
            continue
        tallas = [t.strip().lower() for t in (partes[1] if len(partes) > 1 else "").split(",")
                  if t.strip()]
        obligatorio = len(partes) < 3 or partes[2].lower().startswith("obl")
        if not tallas:
            log(f"Falta la talla en: {url}")
            continue
        for t in tallas:
            articulos.append((url, t, obligatorio))
    return articulos


def consultar(sesion, url):
    r = sesion.get(url, timeout=20)
    r.raise_for_status()
    m = NEXT_DATA.search(r.text)
    if not m:
        raise ValueError("la pagina no trae datos de producto")
    p = json.loads(m.group(1))["props"]["pageProps"]["productData"]["product"]
    stock = {v["size"].lower(): bool(v["inStock"]) for v in p["availableSizes"]}
    variantes = {v["size"].lower(): v["id"] for v in p["availableSizes"]}
    return p["title"], p["colour"], stock, variantes


def descuento_de(sesion, variantes, codigo):
    """Pregunta al pago de Gymshark si el codigo funciona en esa cesta (sin comprar).
    Devuelve el porcentaje (p. ej. 15.0), 0 si no vale, o None si no se pudo saber."""
    if not codigo:
        return 0
    try:
        r = sesion.get(enlace_pago(variantes, codigo), timeout=25)
        r.raise_for_status()
    except Exception as e:
        log(f"No pude comprobar un codigo: {e}")
        return None
    texto = html.unescape(r.text)
    m = re.search(r'"code":"' + re.escape(codigo) + r'"[^{}]*?"value":\{"percentage":([\d.]+)',
                  texto, re.I)
    if m:
        return float(m.group(1))
    return 0 if "checkout" in r.url else None


def enlace_pago(variantes, codigo=""):
    enlace = PAGO + ",".join(f"{v}:1" for v in variantes)
    return enlace + (f"?discount={quote(codigo)}" if codigo else "")


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
    """Aviso urgente repetido sin frenar la vigilancia."""

    def __init__(self):
        self.pendientes = []   # [momento, titulo, texto, url, numero, total, pausa]

    def urgente(self, titulo, texto, url):
        total, pausa = (10, 45) if es_de_noche() else (5, 60)
        self.pendientes.append([time.time(), titulo, texto, url, 1, total, pausa])
        self.procesar()

    def procesar(self):
        for a in list(self.pendientes):
            momento, titulo, texto, url, n, total, pausa = a
            if time.time() < momento:
                continue
            extra = f" (aviso {n}/{total})" if n > 1 else ""
            enviar(titulo, texto + extra, url, 5, "rotating_light")
            if n >= total:
                self.pendientes.remove(a)
            else:
                a[0], a[4] = time.time() + pausa, n + 1


def nombre_de(info, url, talla):
    titulo, color = info[url][:2]
    return f"{titulo} {color.split('/')[0]} {talla.upper()}"


def elegir_codigo(sesion, cesta):
    """Prueba el codigo principal y, si no vale, el de respaldo.
    Devuelve (codigo, descripcion). El valor nunca se escribe en el registro."""
    dudoso = None
    for codigo, nombre in ((CODIGO, "de estudiante"), (RESPALDO, "de respaldo")):
        if not codigo:
            continue
        pct = descuento_de(sesion, cesta, codigo)
        if pct:
            log(f"Codigo {nombre}: funciona ({pct:g} %).")
            return codigo, f"con el codigo {nombre} ({pct:g} %) ya aplicado"
        if pct is None and dudoso is None:
            dudoso = (codigo, f"con el codigo {nombre} (no pude comprobarlo: mira que salga el descuento)")
        log(f"Codigo {nombre}: {'no se pudo comprobar' if pct is None else 'NO funciona'}.")
    return dudoso or ("", "SIN codigo: ninguno funciona, ponlo a mano")


def avisar_pedido(sesion, info, articulos, avisos):
    obligatorios = [(u, t) for u, t, o in articulos if o]
    opcionales = [(u, t) for u, t, o in articulos if not o and info[u][2].get(t)]
    sin_stock = [(u, t) for u, t, o in articulos if not o and not info[u][2].get(t)]
    cesta = [info[u][3][t] for u, t in obligatorios + opcionales]
    lista = "\n".join("• " + nombre_de(info, u, t) for u, t in obligatorios + opcionales)
    faltan = ("\nSin stock (no van): " + ", ".join(nombre_de(info, u, t) for u, t in sin_stock)
              if sin_stock else "")

    # Aviso inmediato con el codigo principal (por si comprobarlo tarda), y luego
    # los repetidos ya con el codigo que de verdad funciona.
    enviar("¡PEDIDO LISTO! Gymshark", "Esta todo lo obligatorio. Pulsa para pagar.",
           enlace_pago(cesta, CODIGO or RESPALDO), 5, "rotating_light")
    codigo, codigo_txt = elegir_codigo(sesion, cesta)
    texto = f"Esta TODO lo obligatorio. Pulsa para pagar {codigo_txt}:\n{lista}{faltan}"
    log("*** PEDIDO LISTO *** " + texto.replace("\n", " | "))
    avisos.urgente("¡PEDIDO LISTO! Gymshark", texto, enlace_pago(cesta, codigo))


class VigilaCodigo:
    """Cada 30 min comprueba que el codigo de estudiante sigue funcionando."""

    def __init__(self):
        self.proxima = 0
        self.funcionaba = True

    def revisar(self, sesion, info, articulos):
        if not CODIGO or time.time() < self.proxima:
            return
        con_stock = [info[u][3][t] for u, t, _ in articulos if u in info and info[u][2].get(t)]
        if not con_stock:
            return
        self.proxima = time.time() + 30 * 60
        pct = descuento_de(sesion, con_stock[:1], CODIGO)
        if pct is None:
            return
        if pct:
            log(f"Codigo de estudiante: sigue funcionando ({pct:g} %).")
            if not self.funcionaba:
                enviar("Codigo de estudiante OK", f"El codigo vuelve a funcionar ({pct:g} %).",
                       "https://eu.gymshark.com/", 2, "white_check_mark")
        else:
            log("Codigo de estudiante: YA NO FUNCIONA.")
            if self.funcionaba:
                enviar("Tu codigo de estudiante ha caducado",
                       "Genera otro y pasaselo a Claude (o cambia el secreto CODIGO_DESCUENTO). "
                       "Mientras, el pedido usara el codigo de respaldo.",
                       "https://eu.gymshark.com/", 2, "warning")
        self.funcionaba = bool(pct)


def main():
    sesion = requests.Session()
    sesion.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    try:
        estado = json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        anterior = estado.get("stock", {})
        pedido_avisado = estado.get("pedido_avisado", False)
        log("Estado anterior recuperado.")
    except Exception:
        anterior, pedido_avisado = {}, False
    avisos = Avisos()
    vigila_codigo = VigilaCodigo()
    fin = time.time() + DURACION
    errores_seguidos = 0
    log(f"Vigilante en marcha. Codigo: {'si' if CODIGO else 'NO'} | "
        f"respaldo: {'si' if RESPALDO else 'NO'}")

    while time.time() < fin:
        articulos = leer_productos()
        info = {}   # url -> (titulo, color, stock, variantes)
        for url in dict.fromkeys(u for u, _, _ in articulos):
            try:
                info[url] = consultar(sesion, url)
                errores_seguidos = 0
            except Exception as e:
                errores_seguidos += 1
                log(f"Error con {url}: {e}")
                if errores_seguidos == 20:
                    enviar("Vigilante Gymshark: problema",
                           f"Gymshark esta rechazando las consultas desde la nube ({e}).",
                           "https://eu.gymshark.com/", 3, "warning")
            avisos.procesar()
            time.sleep(1.5)

        if all(u in info for u, _, _ in articulos):   # solo decide con datos completos
            for u, t, o in articulos:
                antes = anterior.get(u, {}).get(t)
                ahora_hay = info[u][2].get(t, False)
                if antes is None:
                    log(f"{nombre_de(info, u, t)} ({'obligatorio' if o else 'opcional'}): "
                        f"{'HAY stock' if ahora_hay else 'sin stock'}")
                elif ahora_hay and not antes:
                    log(f"Repuesto: {nombre_de(info, u, t)}")
                    pendientes = [nombre_de(info, x, y) for x, y, z in articulos
                                  if z and not info[x][2].get(y)]
                    if pendientes:   # aviso discreto: aun no se puede hacer el pedido
                        enviar(f"Repuesto: {nombre_de(info, u, t)}",
                               "Aun faltan: " + ", ".join(pendientes), u, 2, "eyes")
                elif antes and not ahora_hay:
                    log(f"Agotado: {nombre_de(info, u, t)}")
                    if o:
                        enviar(f"Se ha agotado: {nombre_de(info, u, t)}",
                               "Es obligatorio: el pedido tendra que esperar.", u, 2, "x")

            listo = all(info[u][2].get(t) for u, t, o in articulos if o)
            if listo and not pedido_avisado:
                avisar_pedido(sesion, info, articulos, avisos)
            vigila_codigo.revisar(sesion, info, articulos)
            pedido_avisado = listo
            anterior = {u: info[u][2] for u in info}
            ARCHIVO_ESTADO.write_text(json.dumps(
                {"stock": anterior, "pedido_avisado": pedido_avisado}), encoding="utf-8")

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
