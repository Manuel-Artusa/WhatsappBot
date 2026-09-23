import os
import threading
import traceback
from collections import deque

import requests
from flask import Flask, request

# Importamos todo acá, al arrancar, y no a mitad de una charla: en Python 3.14 los imports
# que se hacen desde varios hilos a la vez pueden quedar trabados esperándose entre sí.
import netrc  # noqa: F401  (lo usa requests por dentro)
import openpyxl  # noqa: F401

import bot
import catalogo

app = Flask(__name__)

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]      # la palabra que inventes (igual que en Meta)
WA_TOKEN = os.environ["WA_TOKEN"]              # token de acceso de Meta
PHONE_ID = os.environ["PHONE_NUMBER_ID"]       # Phone Number ID del número de WhatsApp
GRAPH_URL = f"https://graph.facebook.com/v25.0/{PHONE_ID}/messages"

_http = requests.Session()
_http.trust_env = False  # no busca .netrc ni proxies del sistema (más rápido y sin sorpresas)

_procesados = deque(maxlen=500)  # Meta a veces reenvía el mismo mensaje: lo ignoramos


def normalizar_ar(numero):
    """Los celulares argentinos llegan como 549XXXXXXXXXX.
    Para responder, la API a veces necesita el número sin el 9: 54XXXXXXXXXX."""
    if numero.startswith("549"):
        return "54" + numero[3:]
    return numero


def _graph(payload):
    try:
        r = _http.post(GRAPH_URL, headers={"Authorization": f"Bearer {WA_TOKEN}"}, json=payload, timeout=15)
        if r.status_code != 200:
            print("Error de WhatsApp:", r.status_code, r.text[:300], flush=True)
    except Exception as e:
        print("No se pudo conectar con WhatsApp:", repr(e)[:200], flush=True)


def enviar(to, texto):
    for i in range(0, len(texto), 4000):  # WhatsApp corta en 4096 caracteres
        _graph({"messaging_product": "whatsapp", "to": normalizar_ar(to),
                "type": "text", "text": {"body": texto[i:i + 4000]}})


def marcar_leido_y_escribiendo(message_id):
    """Tildes azules + 'escribiendo...' mientras el bot piensa la respuesta."""
    _graph({"messaging_product": "whatsapp", "status": "read", "message_id": message_id,
            "typing_indicator": {"type": "text"}})


def procesar(msg):
    numero = msg["from"]
    try:
        marcar_leido_y_escribiendo(msg["id"])
        if msg.get("type") == "text":
            respuesta = bot.responder(numero, msg["text"]["body"], avisar=lambda t: enviar(numero, t))
        else:
            respuesta = "Por ahora solo puedo leer mensajes de texto. ¿Me escribís el código o para qué vehículo es?"
        enviar(numero, respuesta)
    except Exception as e:
        print("Error procesando mensaje:", repr(e), flush=True)
        traceback.print_exc()
        enviar(numero, "Perdón, tuve un problema para revisar eso. ¿Me lo repetís en un ratito?")


@app.get("/")
def inicio():
    # Sirve para "despertar" el servidor en Render y chequear que está vivo
    return "Bot activo", 200


@app.get("/webhook")
def verificar():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "token incorrecto", 403


@app.post("/webhook")
def recibir():
    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("id") in _procesados:
                    continue
                _procesados.append(msg.get("id"))
                print("Mensaje recibido de", msg.get("from"), ":", msg.get("text", {}).get("body", msg.get("type")), flush=True)
                # Respondemos a Meta al instante y pensamos la respuesta en segundo plano
                threading.Thread(target=procesar, args=(msg,), daemon=True).start()
    return "ok", 200


_carga_iniciada = False


@app.before_request
def _carga_inicial():
    """La lista se empieza a cargar con la primera visita (Render hace una apenas arranca),
    cuando el servidor ya terminó de iniciarse. Así no hay hilos corriendo mientras se importa."""
    global _carga_iniciada
    if not _carga_iniciada:
        _carga_iniciada = True

        def cargar():
            try:
                catalogo.cargar(forzar=True)
            except Exception as e:
                print("No se pudo cargar la lista al arrancar:", repr(e), flush=True)
        threading.Thread(target=cargar, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
