"""Todo lo que habla con la API de WhatsApp (Meta): mandar textos y archivos, bajar y subir archivos.

Regla de WhatsApp: a un número que NO te escribió en las últimas 24 h no se le puede mandar texto
libre, solo una "plantilla" aprobada. Si pasa eso (error 131047), el mensaje queda guardado en una
cola y se entrega apenas esa persona escriba. Si configurás PLANTILLA_AVISO, además se le manda
esa plantilla para que responda.
"""
import os
import threading

import requests

WA_TOKEN = os.environ.get("WA_TOKEN", "")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID", "")
API = "https://graph.facebook.com/v25.0"
PLANTILLA_AVISO = os.environ.get("PLANTILLA_AVISO", "")          # nombre de una plantilla aprobada (opcional)
PLANTILLA_IDIOMA = os.environ.get("PLANTILLA_IDIOMA", "es_AR")

_http = requests.Session()
_http.trust_env = False
_cola = {}  # numero -> [mensajes pendientes]
_cola_lock = threading.Lock()
# Errores por los que el mensaje se guarda y se entrega cuando esa persona escriba:
# 131047 = pasaron más de 24 h, 131026 = no se pudo entregar, 131030 = número de prueba no autorizado todavía
FUERA_DE_VENTANA = {131047, 131026, 131030}


def normalizar_ar(numero):
    """Los celulares argentinos llegan como 549XXXXXXXXXX.
    Para responder, la API a veces necesita el número sin el 9: 54XXXXXXXXXX."""
    numero = "".join(ch for ch in str(numero) if ch.isdigit())
    if numero.startswith("549"):
        return "54" + numero[3:]
    return numero


def mismo_numero(a, b):
    """Compara números ignorando el +, el 54, el 9 y espacios (mira los últimos 10 dígitos)."""
    a = "".join(ch for ch in str(a) if ch.isdigit())
    b = "".join(ch for ch in str(b) if ch.isdigit())
    return bool(a) and bool(b) and a[-10:] == b[-10:]


def _post(payload):
    """Manda algo a WhatsApp. Devuelve (ok, codigo_de_error)."""
    try:
        r = _http.post(f"{API}/{PHONE_ID}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"},
                       json=payload, timeout=20)
        if r.status_code == 200:
            return True, None
        try:
            codigo = r.json().get("error", {}).get("code")
        except ValueError:
            codigo = None
        print("Error de WhatsApp:", r.status_code, r.text[:300], flush=True)
        return False, codigo
    except Exception as e:
        print("No se pudo conectar con WhatsApp:", repr(e)[:200], flush=True)
        return False, None


def _enviar_o_encolar(numero, payload):
    ok, codigo = _post({"messaging_product": "whatsapp", "to": normalizar_ar(numero), **payload})
    if not ok and codigo in FUERA_DE_VENTANA:
        with _cola_lock:
            primero = numero not in _cola
            _cola.setdefault(numero, []).append(payload)
        print(f"[{numero}] Fuera de las 24 h: mensaje guardado hasta que escriba", flush=True)
        if primero and PLANTILLA_AVISO:
            _post({"messaging_product": "whatsapp", "to": normalizar_ar(numero), "type": "template",
                   "template": {"name": PLANTILLA_AVISO, "language": {"code": PLANTILLA_IDIOMA}}})
    return ok


def entregar_pendientes(numero):
    """Llamar cada vez que alguien escribe: le manda lo que había quedado en cola."""
    with _cola_lock:
        pendientes = _cola.pop(numero, [])
    for p in pendientes:
        _post({"messaging_product": "whatsapp", "to": normalizar_ar(numero), **p})


def enviar(numero, texto):
    for i in range(0, len(texto), 4000):  # WhatsApp corta en 4096 caracteres
        _enviar_o_encolar(numero, {"type": "text", "text": {"body": texto[i:i + 4000]}})


def marcar_leido_y_escribiendo(message_id):
    """Tildes azules + 'escribiendo...' mientras el bot piensa la respuesta."""
    _post({"messaging_product": "whatsapp", "status": "read", "message_id": message_id,
           "typing_indicator": {"type": "text"}})


def descargar_media(media_id):
    """Baja una foto, audio o documento que llegó. Devuelve (bytes, tipo_mime)."""
    h = {"Authorization": f"Bearer {WA_TOKEN}"}
    info = _http.get(f"{API}/{media_id}", headers=h, timeout=15)
    info.raise_for_status()
    info = info.json()
    archivo = _http.get(info["url"], headers=h, timeout=30)
    archivo.raise_for_status()
    return archivo.content, info.get("mime_type", "application/octet-stream").split(";")[0]


def subir_media(datos, mime, nombre_archivo):
    r = _http.post(f"{API}/{PHONE_ID}/media", headers={"Authorization": f"Bearer {WA_TOKEN}"},
                   files={"file": (nombre_archivo, datos, mime)},
                   data={"messaging_product": "whatsapp", "type": mime}, timeout=60)
    r.raise_for_status()
    return r.json()["id"]


def enviar_archivo(numero, datos, mime, nombre_archivo, texto=""):
    """Manda una foto o un documento (PDF) a un número."""
    media_id = subir_media(datos, mime, nombre_archivo)
    if mime.startswith("image/"):
        payload = {"type": "image", "image": {"id": media_id, "caption": texto}}
    else:
        payload = {"type": "document", "document": {"id": media_id, "filename": nombre_archivo, "caption": texto}}
    return _enviar_o_encolar(numero, payload)
