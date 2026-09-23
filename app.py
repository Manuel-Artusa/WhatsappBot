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
import pedidos
import whatsapp
from whatsapp import enviar, descargar_media, marcar_leido_y_escribiendo

app = Flask(__name__)

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]      # la palabra que inventes (igual que en Meta)

# Transcripción de audios (cualquier servicio compatible con la API de OpenAI; por defecto Groq)
TRANSCRIPCION_API_KEY = os.environ.get("TRANSCRIPCION_API_KEY", "")
TRANSCRIPCION_URL = os.environ.get("TRANSCRIPCION_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
TRANSCRIPCION_MODELO = os.environ.get("TRANSCRIPCION_MODELO", "whisper-large-v3-turbo")
MAX_IMAGEN = 5 * 1024 * 1024  # Claude acepta imágenes de hasta 5 MB
IMAGENES = ("image/jpeg", "image/png", "image/webp", "image/gif")

_http = requests.Session()
_http.trust_env = False
_procesados = deque(maxlen=500)  # Meta a veces reenvía el mismo mensaje: lo ignoramos


def transcribir(datos, mime):
    r = _http.post(
        TRANSCRIPCION_URL,
        headers={"Authorization": f"Bearer {TRANSCRIPCION_API_KEY}"},
        files={"file": ("audio.ogg", datos, mime)},
        data={"model": TRANSCRIPCION_MODELO, "language": "es",
              "prompt": "Repuestos diesel: inyector, tobera, bomba, turbo, common rail, Bosch, Denso, Delphi, código."},
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("text", "").strip()


def _texto_de_audio(msg):
    if not TRANSCRIPCION_API_KEY:
        return None
    datos, mime = descargar_media(msg["audio"]["id"])
    texto = transcribir(datos, mime)
    print(f"[{msg['from']}] Audio transcripto: {texto}", flush=True)
    return texto


def _archivo(msg, tipo):
    """(datos, mime, nombre_archivo, texto_que_lo_acompaña) de una foto o documento."""
    parte = msg[tipo]
    datos, mime = descargar_media(parte["id"])
    nombre = parte.get("filename") or ("foto.jpg" if tipo == "image" else "archivo.pdf")
    return datos, mime, nombre, parte.get("caption", "")


def procesar_vendedor(msg):
    numero, tipo = msg["from"], msg.get("type")
    if tipo == "text":
        respuesta = pedidos.mensaje_vendedor(numero, msg["text"]["body"])
    elif tipo in ("image", "document"):
        datos, mime, nombre, texto = _archivo(msg, tipo)
        print(f"[vendedor] Archivo recibido: {nombre} ({mime}, {len(datos) // 1024} KB)", flush=True)
        respuesta = pedidos.mensaje_vendedor(numero, texto, archivo=(datos, mime, nombre))
    elif tipo == "audio":
        texto = _texto_de_audio(msg)
        respuesta = pedidos.mensaje_vendedor(numero, texto) if texto else "No pude escuchar el audio, escribime."
    else:
        respuesta = "Mandame texto, la factura en PDF o foto, o un audio."
    enviar(numero, respuesta)


def procesar_cliente(msg, nombre):
    numero, tipo = msg["from"], msg.get("type")
    avisar = lambda t: enviar(numero, t)  # noqa: E731

    if tipo == "text":
        return bot.responder(numero, msg["text"]["body"], avisar=avisar, nombre=nombre)

    if tipo in ("image", "document"):
        datos, mime, nombre_archivo, texto = _archivo(msg, tipo)
        print(f"[{numero}] {'Foto recibida' if tipo == 'image' else 'Documento recibido'} ({len(datos) // 1024} KB) {texto}", flush=True)
        # ¿Es el comprobante de un pedido que está esperando el pago?
        respuesta = pedidos.revisar_comprobante(numero, datos, mime, nombre_archivo, texto)
        if respuesta:
            bot.nota_en_charla(numero, respuesta)
            return respuesta
        if mime in IMAGENES and len(datos) <= MAX_IMAGEN:
            return bot.responder(numero, texto, avisar=avisar, imagen=(datos, mime), nombre=nombre)
        return "No pude abrir bien ese archivo. ¿Me mandás una foto, o me escribís el código?"

    if tipo == "audio":
        if not TRANSCRIPCION_API_KEY:
            return "Perdón, por acá no puedo escuchar audios. ¿Me lo escribís?"
        texto = _texto_de_audio(msg)
        if not texto:
            return "No llegué a entender el audio. ¿Me lo escribís?"
        return bot.responder(numero, f"(Audio transcripto) {texto}", avisar=avisar, nombre=nombre)

    return "Por ahora puedo leer mensajes de texto, fotos y audios. ¿Me escribís el código o para qué vehículo es?"


def procesar(msg, nombre=None):
    numero = msg["from"]
    try:
        marcar_leido_y_escribiendo(msg["id"])
        whatsapp.entregar_pendientes(numero)  # lo que no se le pudo mandar antes (pasaron 24 h)
        if pedidos.es_vendedor(numero):
            procesar_vendedor(msg)
        else:
            enviar(numero, procesar_cliente(msg, nombre))
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
            valor = change.get("value", {})
            # nombre de perfil de WhatsApp de cada cliente (para agendarlo)
            nombres = {c.get("wa_id"): c.get("profile", {}).get("name") for c in valor.get("contacts", [])}
            for msg in valor.get("messages", []):
                if msg.get("id") in _procesados:
                    continue
                _procesados.append(msg.get("id"))
                print("Mensaje recibido de", msg.get("from"), ":", msg.get("text", {}).get("body", msg.get("type")), flush=True)
                # Respondemos a Meta al instante y pensamos la respuesta en segundo plano
                threading.Thread(target=procesar, args=(msg, nombres.get(msg.get("from"))), daemon=True).start()
    return "ok", 200


_carga_iniciada = False


@app.before_request
def _carga_inicial():
    """La lista y los pedidos abiertos se cargan con la primera visita (Render hace una apenas arranca),
    cuando el servidor ya terminó de iniciarse. Así no hay hilos corriendo mientras se importa."""
    global _carga_iniciada
    if not _carga_iniciada:
        _carga_iniciada = True

        def cargar():
            try:
                catalogo.cargar(forzar=True)
            except Exception as e:
                print("No se pudo cargar la lista al arrancar:", repr(e), flush=True)
            try:
                pedidos.pedidos_de("0")  # trae de la planilla los pedidos abiertos
            except Exception as e:
                print("No se pudieron cargar los pedidos:", repr(e), flush=True)
        threading.Thread(target=cargar, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
