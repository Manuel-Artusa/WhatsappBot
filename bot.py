"""Cerebro del bot: conversa con Claude y usa la lista de precios como herramienta."""
import base64
import faulthandler
import json
import sys
import os
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import requests

import catalogo

_http = requests.Session()
_http.trust_env = False
ZONA = ZoneInfo("America/Argentina/Cordoba")  # se carga al arrancar, no a mitad de una charla

MODELO = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
NOMBRE_NEGOCIO = os.environ.get("NOMBRE_NEGOCIO", "la casa de repuestos")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BUSQUEDA_WEB = os.environ.get("BUSQUEDA_WEB", "1") == "1"  # poné 0 en Render para desactivarla
HORAS_SESION = 12          # después de 12 h sin hablar, la charla arranca de cero
MAX_MENSAJES_HISTORIAL = 30

cliente_ia = anthropic.Anthropic(timeout=45, max_retries=1)  # usa ANTHROPIC_API_KEY
TIEMPO_MAXIMO = 100  # segundos máximos para armar una respuesta
AVISO_ESPERA = 8     # si tarda más que esto, le mandamos "dame un toque" al cliente
_sesiones = {}
_locks = {}
_locks_lock = threading.Lock()

MENSAJE_PARAGUAY = os.environ.get(
    "MENSAJE_PARAGUAY",
    "Hola buenas! Te paso el número de Monse, con ella vas a poder concretar tu venta: +595 994 642911")


def _leer_negocio():
    """Lee negocio.txt: lo que el bot tiene que saber del negocio (editable sin tocar código)."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "negocio.txt")
    try:
        with open(ruta, encoding="utf-8") as f:
            return "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("#")).strip()
    except FileNotFoundError:
        return ""


INFO_NEGOCIO = _leer_negocio()

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _prompt_sistema(sesion):
    ahora = datetime.now(ZONA)
    en_horario = ahora.weekday() < 5 and (9 <= ahora.hour < 13 or 14 <= ahora.hour < 18)
    if sesion["tipo"] == "casa_de_repuestos":
        tipo = f"casa de repuestos ({sesion['negocio']})"
    elif sesion["tipo"] == "particular":
        tipo = "particular / cliente final"
    else:
        tipo = "TODAVÍA NO LO SABÉS"
    return f"""Sos vendedor/a de {NOMBRE_NEGOCIO}, una casa de repuestos de inyección diesel y turbos de Córdoba, Argentina. Atendés por WhatsApp.

SOBRE EL NEGOCIO (esto es lo único que sabés del negocio: no inventes servicios, sucursales ni políticas)
{INFO_NEGOCIO}

PARAGUAY
- Si el cliente es de Paraguay, dice que está en Paraguay o pregunta por Paraguay, respondé EXACTAMENTE este mensaje y nada más: "{MENSAJE_PARAGUAY}". No le cotices ni le pases precios de la lista.

CÓMO HABLÁS
- Como una persona real del mostrador: cordial, directo, en español rioplatense (vos, tenés). Mensajes cortos, como en WhatsApp.
- Nada de listas largas ni títulos. Para resaltar usá *asteriscos* (negrita de WhatsApp). Sin emojis salvo alguno muy ocasional.
- No digas que sos un bot o una IA salvo que te lo pregunten directamente; en ese caso sé honesto.

PASO 1: TIPO DE CLIENTE (antes de cualquier búsqueda o precio)
- Tipo de cliente actual: {tipo}.
- Si no lo sabés, saludá y preguntá si es casa de repuestos o cliente particular.
- Si dice que es casa de repuestos, pedile el nombre del negocio (y localidad) antes de seguir.
- Cuando lo sepas, llamá a registrar_tipo_cliente UNA sola vez. Si arriba ya figura el tipo de cliente, NO lo vuelvas a registrar.

PASO 2: BUSCAR EL REPUESTO
- Pedí el código del producto (el número que tiene grabado la pieza o el código original).
- Si te pasa un código: usá buscar_por_codigo.
  - Si hay "coincidencias", ofrecé ese producto con su precio.
  - "relacionados" son piezas o kits que mencionan ese código (toberas, válvulas, alternativas). Mencionalos solo si sirven.
  - Si no hay coincidencias: usá web_search para averiguar qué códigos son equivalentes o compatibles con ese (cruces de referencia Bosch, Delphi, Denso, Engine Pro, códigos OEM del fabricante del vehículo). Después buscá cada equivalente con buscar_por_codigo. Si encontrás uno, ofrecelo aclarando que es un equivalente y que un vendedor confirma la compatibilidad.
- Si no tiene el código: pedile para qué vehículo es (marca, modelo, año, motor) y qué pieza necesita, y usá buscar_por_vehiculo. Probá con términos cortos (ej: "inyector amarok", "turbo hilux 3.0"). Si hay varias opciones, preguntá lo necesario para identificar la correcta (año, motor, Bosch o Denso, etc.).
- Usá web_search SOLO para buscar equivalencias de un código que no está en la lista. Para búsquedas por vehículo no la uses: si el vehículo no lleva esa pieza (por ejemplo, un motor naftero sin turbo), decíselo o preguntale el motor exacto.
- Si después de buscar no lo tenemos, decilo con naturalidad y ofrecé que un vendedor lo revise.
- NUNCA digas que no tenemos algo, ni inventes políticas del negocio, sin haber buscado antes. En la lista hay productos "USADO PROBADO" (usados y probados, más baratos) además de nuevos: si preguntan por usados, buscá con buscar_por_vehiculo agregando "usado" (ej: "usado inyector a3").
- Cuando ofrezcas un producto, nombralo con el código principal que figura en "codigos" y, si ayuda, el código original del auto que aparece en la descripción.

FOTOS Y AUDIOS
- Si el cliente manda una foto antes de que sepas si es casa de repuestos o particular, leé igual el código y decíselo en tu respuesta (ej: "Veo que es un Bosch *0 445 110 183*"), y después preguntá el tipo de cliente. Nunca digas que estás viendo algo que no ves.
- Si el cliente manda una foto, buscá el código grabado o impreso en la pieza o la etiqueta (ej: Bosch 0 445 110 183, Denso 095000-7760, Delphi, código OEM) y buscalo con buscar_por_codigo. Si no se lee bien, pedile otra foto más de cerca y con buena luz. Si la foto es del vehículo o de otra cosa, usala para entender qué necesita.
- Los audios te llegan transcriptos y pueden tener errores, sobre todo en los códigos. Si el código de un audio no aparece, repetíselo al cliente para confirmarlo o pedile que lo escriba.

PRECIOS Y STOCK
- Usá SOLO los precios que devuelven las herramientas. Nunca inventes ni estimes un precio.
- "a pedido / consultar precio" significa que no hay precio cargado: decí que lo consultás y te lo pasa un vendedor.
- Los precios ya incluyen IVA.
- NUNCA confirmes stock. Decí que un vendedor verifica la disponibilidad.
- No hables de costos, márgenes ni de la existencia de otras listas de precios.

PASO 3: SI QUIERE COMPRAR
- Pedí los datos de facturación (nombre o razón social, CUIT o DNI, condición frente al IVA) y de envío (dirección, localidad, código postal y transporte que prefiere, o si retira).
- Con esos datos, llamá a pasar_a_vendedor con un resumen del pedido, y avisale que un vendedor le confirma stock y el total.

HORARIO
- Ahora es {DIAS[ahora.weekday()]} {ahora:%d/%m %H:%M}. La atención de vendedores es de lunes a viernes de 9 a 13 y de 14 a 18.
- {"Estamos en horario: un vendedor puede responder hoy." if en_horario else "Estamos FUERA de horario: podés buscar y cotizar igual, pero aclarale que la confirmación de un vendedor llega el próximo día hábil."}
"""


HERRAMIENTAS = [
    {
        "name": "registrar_tipo_cliente",
        "description": "Registra si el cliente es casa de repuestos o particular. Obligatorio antes de buscar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["casa_de_repuestos", "particular"]},
                "nombre_negocio": {"type": "string", "description": "Nombre y localidad del negocio si es casa de repuestos"},
            },
            "required": ["tipo"],
        },
    },
    {
        "name": "buscar_por_codigo",
        "description": "Busca en la lista de precios por código de pieza, código de sistema o código OEM. Acepta códigos parciales (ej. '110183').",
        "input_schema": {
            "type": "object",
            "properties": {"codigo": {"type": "string"}},
            "required": ["codigo"],
        },
    },
    {
        "name": "buscar_por_vehiculo",
        "description": "Busca en la lista de precios por vehículo, motor y/o tipo de pieza. Usá 2 a 4 palabras clave, sin el año ni la marca del auto si el modelo ya la identifica. Ej: 'inyector hilux 2.5', 'inyector kangoo euro 4', 'bomba alta s10 2.8'. Después usá el año y el motor para elegir entre los resultados.",
        "input_schema": {
            "type": "object",
            "properties": {"consulta": {"type": "string"}},
            "required": ["consulta"],
        },
    },
    {
        "name": "pasar_a_vendedor",
        "description": "Avisa a un vendedor humano: pedido listo, precio a consultar o consulta que no se pudo resolver.",
        "input_schema": {
            "type": "object",
            "properties": {"resumen": {"type": "string", "description": "Qué necesita el cliente, productos, datos de factura y envío"}},
            "required": ["resumen"],
        },
    },
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
]


def _herramientas(sesion):
    """Si ya sabemos el tipo de cliente, no le ofrecemos registrarlo de nuevo."""
    hs = [h for h in HERRAMIENTAS if not (sesion["tipo"] and h["name"] == "registrar_tipo_cliente")]
    if not BUSQUEDA_WEB:
        hs = [h for h in hs if h["name"] != "web_search"]
    return hs


def _avisar_vendedor(numero, sesion, resumen):
    texto = f"Nuevo aviso del bot de WhatsApp\nCliente: +{numero}\nTipo: {sesion['tipo']} {sesion['negocio'] or ''}\n\n{resumen}"
    print("PASAR A VENDEDOR:", texto, flush=True)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            _http.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": texto}, timeout=10)
        except Exception as e:
            print("Error avisando por Telegram:", e, flush=True)


def _ejecutar(nombre, datos, numero, sesion):
    if nombre == "registrar_tipo_cliente":
        sesion["tipo"] = datos["tipo"]
        sesion["negocio"] = datos.get("nombre_negocio")
        return {"ok": True, "nota": "Registrado. No hace falta volver a registrarlo en esta charla."}
    if nombre in ("buscar_por_codigo", "buscar_por_vehiculo") and not sesion["tipo"]:
        return {"error": "Primero preguntá si es casa de repuestos o particular y llamá a registrar_tipo_cliente."}
    if nombre == "buscar_por_codigo":
        return catalogo.buscar_por_codigo(datos["codigo"], sesion["tipo"])
    if nombre == "buscar_por_vehiculo":
        res = catalogo.buscar_por_texto(datos["consulta"], sesion["tipo"])
        return {"resultados": res} if res else {"resultados": [], "nota": "Sin resultados. Probá con menos palabras o sinónimos."}
    if nombre == "pasar_a_vendedor":
        _avisar_vendedor(numero, sesion, datos["resumen"])
        return {"ok": True}
    return {"error": f"herramienta desconocida: {nombre}"}


def _ejecutar_con_tope(nombre, datos, numero, sesion, tope=15):
    """Corre la herramienta, pero si tarda más de `tope` segundos no espera más."""
    res = {}

    def correr():
        try:
            res["salida"] = _ejecutar(nombre, datos, numero, sesion)
        except Exception as e:
            print("Error en herramienta", nombre, repr(e), flush=True)
            res["salida"] = {"error": "No se pudo consultar la lista en este momento."}

    t = threading.Thread(target=correr, daemon=True)
    t.start()
    t.join(tope)
    if t.is_alive():
        print(f"[{numero}] La herramienta {nombre} tardó más de {tope}s", flush=True)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        return {"error": "La búsqueda está tardando. Decile al cliente que lo revisa un vendedor."}
    return res["salida"]


def _sesion(numero):
    s = _sesiones.get(numero)
    if not s or time.time() - s["ultima"] > HORAS_SESION * 3600:
        s = {"historial": [], "tipo": None, "negocio": None, "ultima": time.time()}
        _sesiones[numero] = s
    s["ultima"] = time.time()
    return s


def _lock_de(numero):
    with _locks_lock:
        return _locks.setdefault(numero, threading.Lock())


def _formato_whatsapp(texto):
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)        # **negrita** -> *negrita*
    texto = re.sub(r"^#+\s*", "", texto, flags=re.MULTILINE)  # sin títulos markdown
    return texto.strip()


def responder(numero, texto_usuario, avisar=None, imagen=None):
    """Recibe el mensaje del cliente y devuelve el texto de respuesta.
    avisar(texto): función opcional para mandar un mensaje intermedio si la respuesta tarda.
    imagen: (bytes, tipo_mime) si el cliente mandó una foto."""
    # Números de Paraguay (+595): directo al contacto de Paraguay, sin pasar por Claude
    if numero.startswith("595"):
        print(f"[{numero}] Número de Paraguay: derivado a Monse", flush=True)
        return MENSAJE_PARAGUAY

    with _lock_de(numero):  # un mensaje por vez por cliente
        sesion = _sesion(numero)
        # La foto se guarda un par de mensajes más, por si el bot todavía no pudo buscar
        # (por ejemplo, porque primero tenía que preguntar si es casa de repuestos o particular).
        if imagen:
            sesion["imagen"] = {"datos": imagen[0], "mime": imagen[1], "turnos": 3}
            nota = texto_usuario or "(El cliente mandó esta foto, sin texto.)"
            texto_historial = f"[Mandó una foto] {texto_usuario}".strip()
        else:
            nota = texto_usuario
            texto_historial = texto_usuario
        foto = sesion.get("imagen")
        if foto and foto["turnos"] > 0:
            foto["turnos"] -= 1
            if not imagen:
                nota = f"(Te vuelvo a pasar la foto que el cliente mandó antes.)\n{texto_usuario}"
            contenido = [
                {"type": "image", "source": {"type": "base64", "media_type": foto["mime"],
                                             "data": base64.standard_b64encode(foto["datos"]).decode()}},
                {"type": "text", "text": nota},
            ]
        else:
            sesion.pop("imagen", None)
            contenido = texto_usuario
        mensajes = sesion["historial"] + [{"role": "user", "content": contenido}]
        inicio = time.time()
        aviso = None
        if avisar:
            aviso = threading.Timer(AVISO_ESPERA, avisar, args=("Dame un toque que lo reviso y te digo.",))
            aviso.start()

        faulthandler.dump_traceback_later(30, repeat=False, file=sys.stderr)
        r = None
        try:
            for vuelta in range(10):  # tope de vueltas por seguridad
                if time.time() - inicio > TIEMPO_MAXIMO:
                    print(f"[{numero}] Se pasó del tiempo máximo, corto.", flush=True)
                    r = None
                    break
                print(f"[{numero}] Consultando a Claude (vuelta {vuelta + 1})", flush=True)
                r = cliente_ia.messages.create(
                    model=MODELO,
                    max_tokens=1024,
                    system=_prompt_sistema(sesion),
                    tools=_herramientas(sesion),
                    messages=mensajes,
                )
                for b in r.content:
                    if b.type == "server_tool_use":
                        print(f"[{numero}] Búsqueda web: {b.input}", flush=True)
                mensajes.append({"role": "assistant", "content": r.content})
                if r.stop_reason == "pause_turn":  # la búsqueda web sigue en curso
                    continue
                if r.stop_reason != "tool_use":
                    break
                resultados = []
                for b in r.content:
                    if b.type == "tool_use":
                        print(f"[{numero}] {b.name}({b.input})", flush=True)
                        t0 = time.time()
                        salida = _ejecutar_con_tope(b.name, b.input, numero, sesion)
                        if b.name.startswith("buscar"):
                            sesion.pop("imagen", None)  # ya buscó: no hace falta seguir mandando la foto
                        print(f"[{numero}] {b.name} listo en {time.time() - t0:.2f}s", flush=True)
                        resultados.append({"type": "tool_result", "tool_use_id": b.id,
                                           "content": json.dumps(salida, ensure_ascii=False)})
                mensajes.append({"role": "user", "content": resultados})
        finally:
            faulthandler.cancel_dump_traceback_later()
            if aviso:
                aviso.cancel()

        respuesta = ""
        if r is not None and r.stop_reason not in ("tool_use", "pause_turn"):
            respuesta = _formato_whatsapp("".join(b.text for b in r.content if b.type == "text"))
        if not respuesta:
            respuesta = "Lo estoy revisando con un compañero y en un ratito te confirmo."
            _avisar_vendedor(numero, sesion, f"El bot no pudo resolver esta consulta a tiempo. Último mensaje del cliente: {texto_usuario}")

        # En el historial guardamos solo el texto de la charla (sin las búsquedas internas)
        sesion["historial"] += [{"role": "user", "content": texto_historial},
                                {"role": "assistant", "content": respuesta}]
        sesion["historial"] = sesion["historial"][-MAX_MENSAJES_HISTORIAL:]
        print(f"[{numero}] Respuesta: {respuesta[:300]}", flush=True)
        return respuesta
