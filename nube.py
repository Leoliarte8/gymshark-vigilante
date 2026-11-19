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
import threading
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
    return ahora().hour < 8   # de 00:00 a 8:00


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
    precios = {v["size"].lower(): _precio(v, p) for v in p["availableSizes"]}
    unidades = {v["size"].lower(): v.get("inventoryQuantity") for v in p["availableSizes"]}
    return p["title"], p["colour"], stock, variantes, precios, unidades


def _precio(v, p):
    """El precio es solo informativo: si viene raro, 0 (nunca rompe la lectura del stock)."""
    try:
        return float(str(v.get("price") or p.get("price") or 0).replace(",", ".").strip(" €$"))
    except (TypeError, ValueError):
        return 0.0


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


RAFAGA = 3   # avisos seguidos en cada repeticion de noche


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


# Llamada de telefono de verdad (Twilio): vibra sin parar mientras suena.
# Secretos: TWILIO_SID, TWILIO_TOKEN, TWILIO_DESDE (numero de Twilio), TELEFONO (el tuyo).
TWILIO_SID = os.environ.get("TWILIO_SID", "").strip()
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "").strip()
TWILIO_DESDE = os.environ.get("TWILIO_DESDE", "").strip()
TELEFONO = os.environ.get("TELEFONO", "").strip()
LLAMADAS_ACTIVAS = all((TWILIO_SID, TWILIO_TOKEN, TWILIO_DESDE, TELEFONO))
LLAMADAS_MAX = 5
LLAMADAS_CADA = 120   # segundos entre llamadas (2 llamadas en <3 min saltan el modo descanso)
REINTENTO_LLAMADA = 45   # segundos si la llamada no se pudo pedir


def llamar(texto):
    """Pide a Twilio que llame a tu movil. True si la ha aceptado."""
    if not LLAMADAS_ACTIVAS:
        return True
    voz = html.escape(texto[:300])
    twiml = (f'<Response><Say language="es-ES">{voz}</Say><Pause length="2"/>'
             f'<Say language="es-ES">{voz}</Say></Response>')
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json",
            auth=(TWILIO_SID, TWILIO_TOKEN), timeout=30,
            data={"To": TELEFONO, "From": TWILIO_DESDE, "Twiml": twiml, "Timeout": "60"})
    except Exception as e:   # sin detalles: podrian llevar datos de la cuenta
        log(f"No se pudo pedir la llamada: {type(e).__name__}")
        return False
    if r.status_code not in (200, 201):
        log(f"Twilio no ha aceptado la llamada (respuesta {r.status_code}).")
        return False
    log("Llamada de telefono pedida.")
    return True


class Avisos:
    """Aviso urgente repetido sin frenar la vigilancia (se puede parar desde el movil).
    De noche, ademas, llamadas de telefono cada 2 min hasta que pulses parar."""

    def __init__(self):
        self.pendientes = []   # [momento, titulo, texto, url, numero, total, pausa]
        self.desde = 0         # cuando empezo la tanda de avisos actual
        self.llamadas = 0
        self.intentos = 0
        self.llamando = False
        self.proxima_llamada = 0
        self.texto_llamada = ""

    def urgente(self, titulo, texto, url):
        # De noche: rafagas de 3 avisos cada 20 s durante 10 min (hasta que pulses parar).
        total, pausa = (30, 20) if es_de_noche() else (5, 60)
        if not self.pendientes:
            self.desde = time.time() - 30   # incluye el aviso inmediato previo
            self.llamadas = 0 if es_de_noche() else LLAMADAS_MAX   # de dia no llama
            self.intentos = 0
            self.proxima_llamada = time.time()
            self.texto_llamada = (f"{titulo.replace('¡', '')} Despierta, hay reestock en Gymshark. "
                                  "Mira las notificaciones del movil.")
        self.pendientes.append([time.time(), titulo, texto, url, 1, total, pausa])
        self.procesar()

    def procesar(self):
        listos = [a for a in self.pendientes if time.time() >= a[0]]
        toca_llamar = (self.pendientes and self.llamadas < LLAMADAS_MAX
                       and time.time() >= self.proxima_llamada)
        if (any(a[4] > 1 for a in listos) or (toca_llamar and self.llamadas)) \
                and han_pedido_parar(self.desde):
            log("Has pulsado parar en el movil: dejo de repetir los avisos y las llamadas.")
            self.pendientes.clear()
            return
        if toca_llamar and not self.llamando:
            self.intentos += 1
            self.llamando = True
            self.proxima_llamada = float("inf")   # se fija al acabar la llamada

            def hacer_llamada():   # en segundo plano: CallMeBot tarda hasta 1 min
                try:
                    if llamar(self.texto_llamada):
                        self.llamadas += 1
                        self.proxima_llamada = time.time() + LLAMADAS_CADA
                    elif self.intentos >= 12:   # CallMeBot no responde: lo dejo (ntfy sigue)
                        self.llamadas = LLAMADAS_MAX
                    else:
                        self.proxima_llamada = time.time() + REINTENTO_LLAMADA
                finally:
                    self.llamando = False
            threading.Thread(target=hacer_llamada, daemon=True).start()
        for a in listos:
            momento, titulo, texto, url, n, total, pausa = a
            extra = f" (aviso {n}/{total}, pulsa parar para callarlo)" if n > 1 else ""
            for i in range(RAFAGA if es_de_noche() else 1):   # varias vibraciones seguidas
                if i:
                    time.sleep(1.5)
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
            return codigo, f"con el codigo {nombre} ({pct:g} %) ya aplicado", pct
        if pct is None and dudoso is None:
            dudoso = (codigo, f"con el codigo {nombre} (no pude comprobarlo: mira que salga el descuento)", None)
        log(f"Codigo {nombre}: {'no se pudo comprobar' if pct is None else 'NO funciona'}.")
    return dudoso or ("", "SIN codigo: ninguno funciona, ponlo a mano", 0)


# Avisos urgentes (dia y noche), por orden de importancia. Cada uno salta cuando
# su condicion pasa de no cumplirse a cumplirse. Una condicion es una lista de
# (trozo de la URL del producto, talla) que tienen que tener stock A LA VEZ;
# None = todos los obligatorios de productos.txt.
def urgentes(info, articulos):
    """Avisos urgentes: el pedido completo, y cualquier obligatorio que vuelva."""
    lista = [("¡PEDIDO COMPLETO! Gymshark", "Esta todo lo importante a la vez", None)]
    for u, t, o in articulos:
        if o:
            nombre = nombre_de(info, u, t)
            lista.append((f"¡Ha vuelto: {nombre}!", f"{nombre} repuesta", [(u, t)]))
    return lista


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


def euros(x):
    return f"{x:.2f}".replace(".", ",") + " €"


def precio_obligatorio(info, articulos, pct):
    """Total de las cosas obligatorias (sin opcionales), con y sin descuento."""
    disponibles = [(u, t) for u, t, o in articulos if o and info[u][2].get(t)]
    faltan = sum(1 for u, t, o in articulos if o and not info[u][2].get(t))
    precios = [info[u][4].get(t, 0) for u, t in disponibles]
    if not precios:
        return f"💶 De los obligatorios aún no hay ninguno con stock (faltan {faltan})"
    if not all(precios):
        return "💶 Total obligatorio: no lo pude calcular (míralo en la cesta)"
    total = sum(precios)
    if pct and 0 < pct <= 100:
        txt = (f"💶 A PAGAR con el código (-{pct:g} %): {euros(total * (1 - pct / 100))}"
               f" (sin código: {euros(total)}")
    else:
        txt = f"💶 A PAGAR (sin código de descuento): {euros(total)} ("
    txt += ("; " if pct and 0 < pct <= 100 else "") + "obligatorios disponibles, sin opcionales ni envío"
    return txt + (f"; faltan {faltan} obligatorios sin stock)" if faltan else ")")


def precio_cesta(info, en_cesta, pct):
    """Total de todo lo que va en la cesta preparada (opcionales incluidos)."""
    precios = [info[u][4].get(t, 0) for u, t in en_cesta]
    if not precios or not all(precios):
        return "🛒 Total de la cesta: míralo al abrirla"
    total = sum(precios)
    if pct and 0 < pct <= 100:
        return (f"🛒 TOTAL CESTA con el código (-{pct:g} %): {euros(total * (1 - pct / 100))}"
                f" (sin código: {euros(total)}; sin envío)")
    return f"🛒 TOTAL CESTA (sin código): {euros(total)} (sin envío)"


def avisar_urgente(sesion, info, articulos, avisos, titulo, motivos, solo=None):
    """Aviso urgente con la cesta preparada: todo lo vigilado que tenga stock ahora
    (o, si se pasa `solo`, unicamente esas cosas)."""
    if solo:
        articulos = [(u, t, o) for u, t, o in articulos
                     if any(trozo in u and t == talla for trozo, talla in solo)]
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
    codigo, codigo_txt, pct = elegir_codigo(sesion, cesta)
    obligatorio = "" if solo else precio_obligatorio(info, articulos, pct) + "\n"
    texto = (f"{motivos}.\n{precio_cesta(info, en_cesta, pct)}\n{obligatorio}"
             f"Cesta preparada {codigo_txt}:\n{lista}{faltan_txt}")
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


POCAS_UNIDADES = 15   # aviso cuando algo con stock baja de estas unidades


def unidades_de(info, u, t):
    """Unidades que quedan, o None si Gymshark no da una cifra valida."""
    n = info[u][5].get(t)
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return None
    try:
        return max(int(n), 0)
    except (ValueError, OverflowError):
        return None


def revisar_pocas_unidades(info, articulos, avisadas):
    """Avisa (una vez) cuando algo de la lista con stock baja de POCAS_UNIDADES.
    De noche no avisa: lo hara por la manana si sigue igual."""
    for u, t, o in articulos:
        clave = f"{u}|{t}"
        n = unidades_de(info, u, t)
        if n is None:
            continue
        if not info[u][2].get(t) or n >= POCAS_UNIDADES:
            avisadas.discard(clave)   # se ha agotado o se ha repuesto: se puede volver a avisar
            continue
        if clave in avisadas or es_de_noche():
            continue
        avisadas.add(clave)
        log(f"Quedan pocas: {nombre_de(info, u, t)} ({n})")
        enviar(f"⏳ Quedan pocas: {nombre_de(info, u, t)}",
               f"Solo quedan {n} unidades{'' if o else ' (es opcional)'}. "
               "Si lo quieres asegurar, compralo ya.", u, 4, "hourglass")


LATIDO_CADA = 30 * 60   # aviso de "sigo vivo" (solo de dia)


def latido(info, articulos):
    """Aviso de prioridad minima para saber que el vigilante sigue funcionando."""
    def unidades(u, t):
        n = info[u][5].get(t)
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return ""
        try:
            return f" · {max(int(n), 0)} uds"
        except (ValueError, OverflowError):
            return ""

    lineas = [("✅ " if info[u][2].get(t) else "❌ ") + nombre_de(info, u, t)
              + unidades(u, t) + ("" if o else " (opc.)") for u, t, o in articulos]
    log("Latido: sigo vigilando.")
    enviar(f"Sigo vigilando · {ahora():%H:%M}", "\n".join(lineas),
           "https://github.com/Leoliarte8/gymshark-vigilante/actions", 1, "green_heart")


def main():
    sesion = requests.Session()
    sesion.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    try:
        estado = json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        anterior = estado.get("stock", {})
        ultimo_latido = estado.get("latido", 0)
        pocas_avisadas = set(estado.get("pocas", []))
        log("Estado anterior recuperado.")
    except Exception:
        anterior, ultimo_latido, pocas_avisadas = {}, 0, set()
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
                if errores_seguidos >= 20 and not problema_avisado:   # tambien de noche
                    problema_avisado = True
                    enviar("⚠️ Vigilante Gymshark: problema",
                           f"Gymshark esta rechazando las consultas ({type(e).__name__}). "
                           "Mientras dure, no puedo vigilar.",
                           "https://github.com/Leoliarte8/gymshark-vigilante/actions", 4, "warning")
            avisos.procesar()
            time.sleep(1.5)

        if all(u in info for u, _, _ in articulos):   # solo decide con datos completos
            # 1) Avisos urgentes: se manda solo el mas importante de los que salten.
            disparados = []
            URGENTES = urgentes(info, articulos)
            for titulo, motivo, condicion, *solo in URGENTES:
                ahora_si = condicion_cumplida(condicion, articulos,
                                              lambda u, t: info[u][2].get(t, False))
                antes_si = condicion_cumplida(condicion, articulos,
                                              lambda u, t: anterior.get(u, {}).get(t))
                if ahora_si and antes_si is False:
                    disparados.append((titulo, motivo, solo[0] if solo else None))
            if disparados:
                completo = disparados[0][0] == URGENTES[0][0]
                motivos = disparados[0][1] if completo else " + ".join(m for _, m, _ in disparados)
                # cesta reducida solo si es el unico aviso que salta
                solo = disparados[0][2] if len(disparados) == 1 else None
                avisar_urgente(sesion, info, articulos, avisos, disparados[0][0], motivos, solo)

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
            revisar_pocas_unidades(info, articulos, pocas_avisadas)
            if time.time() - ultimo_latido >= LATIDO_CADA and not es_de_noche():
                ultimo_latido = time.time()
                try:   # el aviso de "sigo vivo" nunca debe parar la vigilancia
                    latido(info, articulos)
                except Exception as e:
                    log(f"No pude mandar el aviso de 'sigo vivo': {type(e).__name__}")
            anterior = {u: info[u][2] for u in info}
            ARCHIVO_ESTADO.write_text(json.dumps({"stock": anterior, "latido": ultimo_latido,
                                                  "pocas": sorted(pocas_avisadas)}),
                                      encoding="utf-8")

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
