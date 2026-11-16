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
    except Exception as e:   # el mensaje del error lleva el enlace con el codigo: no se muestra
        estado = getattr(getattr(e, "response", None), "status_code", "")
        log(f"No pude comprobar un codigo: {type(e).__name__} {estado}".strip())
        return None
    texto = html.unescape(r.text)
    m = re.search(r'"code":"' + re.escape(codigo) + r'"[^{}]*?"value":\{"percentage":([\d.]+)',
                  texto, re.I)
    if m:
        return float(m.group(1))
    # Solo se da por "no vale" si es de verdad la pagina de pago con su bloque de
    # descuentos; cualquier otra cosa (bloqueo, pagina rara) es "no lo se".
    es_pago = urlparse(r.url).netloc == "eu.checkout.gymshark.com" and "/checkouts/" in r.url
    return 0 if es_pago and '"merchandiseDiscount"' in texto else None


def enlace_pago(variantes, codigo=""):
    enlace = PAGO + ",".join(f"{v}:1" for v in variantes)
    return enlace + (f"?discount={quote(codigo)}" if codigo else "")


def canal_parar():
    """Canal al que el boton "parar" del movil manda un mensaje."""
    return CANAL + "-parar"


def acciones_urgentes(url):
    return [
        {"action": "view", "label": "Abrir cesta", "url": url},
        {"action": "view", "label": "Ya estoy despierto, parar",
         "url": f"https://ntfy.sh/{canal_parar()}/publish?message=parar", "clear": True},
    ]


def enviar(titulo, texto, url, prioridad, etiqueta, acciones=None):
    if not CANAL:
        log("Falta el secreto NTFY_TOPIC: no puedo avisar al movil.")
        return
    datos = {"topic": CANAL, "title": titulo, "message": texto, "click": url,
             "priority": prioridad, "tags": [etiqueta]}
    if acciones:
        datos["actions"] = acciones
    try:
        requests.post("https://ntfy.sh/", timeout=10, json=datos).raise_for_status()
    except Exception as e:
        log(f"No se pudo avisar al movil: {type(e).__name__}")


def han_pedido_parar(desde):
    """True si desde el movil se ha pulsado "parar" despues de 'desde'."""
    if not CANAL:
        return False
    try:
        r = requests.get(f"https://ntfy.sh/{canal_parar()}/json",
                         params={"poll": "1", "since": str(int(desde))}, timeout=10)
        r.raise_for_status()
        return any('"event":"message"' in linea for linea in r.text.splitlines())
    except Exception as e:
        log(f"No pude mirar si has pedido parar: {type(e).__name__}")
        return False


def enviar_urgente(titulo, texto, url):
    enviar(titulo, texto, url, 5, "rotating_light", acciones_urgentes(url))


class Avisos:
    """Aviso urgente repetido sin frenar la vigilancia (se puede parar desde el movil)."""

    def __init__(self):
        self.pendientes = []   # [momento, titulo, texto, url, numero, total, pausa]
        self.desde = 0         # cuando empezo la tanda de avisos actual

    def urgente(self, titulo, texto, url):
        total, pausa = (10, 45) if es_de_noche() else (5, 60)
        if not self.pendientes:
            self.desde = time.time() - 30   # incluye el aviso inmediato previo
        self.pendientes.append([time.time(), titulo, texto, url, 1, total, pausa])
        self.procesar()

    def procesar(self):
        listos = [a for a in self.pendientes if time.time() >= a[0]]
        if any(a[4] > 1 for a in listos) and han_pedido_parar(self.desde):
            log("Has pulsado parar en el movil: dejo de repetir los avisos.")
            self.pendientes.clear()
            return
        for a in listos:
            momento, titulo, texto, url, n, total, pausa = a
            extra = f" (aviso {n}/{total})" if n > 1 else ""
            enviar_urgente(titulo, texto + extra, url)
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


# Avisos urgentes (dia y noche), por orden de importancia. Cada uno salta cuando
# su condicion pasa de no cumplirse a cumplirse. Una condicion es una lista de
# (trozo de la URL del producto, talla) que tienen que tener stock A LA VEZ;
# None = todos los obligatorios de productos.txt.
URGENTES = [
    ("¡PEDIDO COMPLETO! Gymshark", "Esta todo lo importante a la vez", None),
    ("¡Ha vuelto la Vengeance negra!", "Vengeance negra XS repuesta",
     [("vengeance-t-shirt-ss-tops-black", "xs")]),
    ("¡Ha vuelto la Vengeance gris!", "Vengeance gris XS repuesta",
     [("vengeance-t-shirt-ss-tops-grey", "xs")]),
    ("¡Pantalon y Collegiate a la vez!", "Pumper Pants XS y Collegiate Tech Hoodie XS con stock",
     [("straight-leg-pumper-pants", "xs"), ("collegiate-tech-hoodie", "xs")]),
]


def condicion_cumplida(condicion, articulos, stock_de):
    """stock_de(url, talla) -> True/False/None. Devuelve True/False, o None si falta algun dato."""
    if condicion is None:
        elegidos = [(u, t) for u, t, o in articulos if o]
    else:
        elegidos = [(u, t) for u, t, _ in articulos
                    if any(trozo in u and t == talla for trozo, talla in condicion)]
    if not elegidos:
        return False
    valores = [stock_de(u, t) for u, t in elegidos]
    if any(v is None for v in valores):
        return None
    return all(valores)


def avisar_urgente(sesion, info, articulos, avisos, titulo, motivos):
    """Aviso urgente con la cesta preparada: todo lo vigilado que tenga stock ahora."""
    en_cesta = [(u, t) for u, t, _ in articulos if info[u][2].get(t)]
    faltan = [(u, t, o) for u, t, o in articulos if not info[u][2].get(t)]
    cesta = [info[u][3][t] for u, t in en_cesta]
    lista = "\n".join("• " + nombre_de(info, u, t) for u, t in en_cesta)
    faltan_txt = ""
    if faltan:
        faltan_txt = "\nSin stock: " + ", ".join(
            nombre_de(info, u, t) + ("" if o else " (opcional)") for u, t, o in faltan)

    # Aviso inmediato con el codigo principal (por si comprobarlo tarda), y luego
    # los repetidos ya con el codigo que de verdad funciona.
    enviar_urgente(titulo, motivos + ". Pulsa para pagar.", enlace_pago(cesta, CODIGO or RESPALDO))
    codigo, codigo_txt = elegir_codigo(sesion, cesta)
    texto = f"{motivos}. Cesta preparada {codigo_txt}:\n{lista}{faltan_txt}"
    log(f"*** {titulo} *** " + texto.replace("\n", " | "))
    avisos.urgente(titulo, texto, enlace_pago(cesta, codigo))


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
                if not es_de_noche():
                    enviar("Codigo de estudiante OK", f"El codigo vuelve a funcionar ({pct:g} %).",
                           "https://eu.gymshark.com/", 2, "white_check_mark")
        else:
            log("Codigo de estudiante: YA NO FUNCIONA.")
            if es_de_noche():
                return   # se volvera a mirar y avisar de dia (no se marca como avisado)
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
        log("Estado anterior recuperado.")
    except Exception:
        anterior = {}
    avisos = Avisos()
    vigila_codigo = VigilaCodigo()
    fin = time.time() + DURACION
    errores_seguidos, problema_avisado = 0, False
    log(f"Vigilante en marcha. Codigo: {'si' if CODIGO else 'NO'} | "
        f"respaldo: {'si' if RESPALDO else 'NO'}")

    while time.time() < fin:
        articulos = leer_productos()
        info = {}   # url -> (titulo, color, stock, variantes)
        for url in dict.fromkeys(u for u, _, _ in articulos):
            try:
                info[url] = consultar(sesion, url)
                errores_seguidos, problema_avisado = 0, False
            except Exception as e:
                errores_seguidos += 1
                log(f"Error con {url}: {e}")
                if errores_seguidos >= 20 and not problema_avisado and not es_de_noche():
                    problema_avisado = True
                    enviar("Vigilante Gymshark: problema",
                           f"Gymshark esta rechazando las consultas desde la nube ({e}).",
                           "https://eu.gymshark.com/", 3, "warning")
            avisos.procesar()
            time.sleep(1.5)

        if all(u in info for u, _, _ in articulos):   # solo decide con datos completos
            # 1) Avisos urgentes: se manda solo el mas importante de los que salten.
            disparados = []
            for titulo, motivo, condicion in URGENTES:
                ahora_si = condicion_cumplida(condicion, articulos,
                                              lambda u, t: info[u][2].get(t, False))
                antes_si = condicion_cumplida(condicion, articulos,
                                              lambda u, t: anterior.get(u, {}).get(t))
                if ahora_si and antes_si is False:
                    disparados.append((titulo, motivo))
            if disparados:
                completo = disparados[0][0] == URGENTES[0][0]
                motivos = disparados[0][1] if completo else " + ".join(m for _, m in disparados)
                avisar_urgente(sesion, info, articulos, avisos, disparados[0][0], motivos)

            # 2) Avisos discretos: solo de dia y si no ha saltado uno urgente.
            discretos = not disparados and not es_de_noche()
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
                    if pendientes and discretos:
                        enviar(f"Repuesto: {nombre_de(info, u, t)}",
                               "Aun faltan: " + ", ".join(pendientes), u, 2, "eyes")
                elif antes and not ahora_hay:
                    log(f"Agotado: {nombre_de(info, u, t)}")
                    if o and discretos:
                        enviar(f"Se ha agotado: {nombre_de(info, u, t)}",
                               "Es obligatorio: el pedido tendra que esperar.", u, 2, "x")

            vigila_codigo.revisar(sesion, info, articulos)
            anterior = {u: info[u][2] for u in info}
            ARCHIVO_ESTADO.write_text(json.dumps({"stock": anterior}), encoding="utf-8")

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
