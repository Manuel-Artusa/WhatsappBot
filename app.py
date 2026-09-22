import os
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]      # la palabra que inventes (igual que en Meta)
WA_TOKEN = os.environ["WA_TOKEN"]              # token de acceso de Meta
PHONE_ID = os.environ["PHONE_NUMBER_ID"]       # Phone Number ID del número de WhatsApp
GRAPH_URL = f"https://graph.facebook.com/v25.0/{PHONE_ID}/messages"


def normalizar_ar(numero):
    """Los celulares argentinos llegan como 549XXXXXXXXXX.
    Para responder, la API a veces necesita el número sin el 9: 54XXXXXXXXXX."""
    if numero.startswith("549"):
        return "54" + numero[3:]
    return numero


def enviar(to, texto):
    r = requests.post(
        GRAPH_URL,
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        json={
            "messaging_product": "whatsapp",
            "to": normalizar_ar(to),
            "type": "text",
            "text": {"body": texto},
        },
        timeout=15,
    )
    if r.status_code != 200:
        print("Error al enviar:", r.status_code, r.text, flush=True)


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
                numero = msg.get("from")
                print("Mensaje recibido de", numero, ":", msg, flush=True)
                if msg.get("type") == "text":
                    enviar(numero, "Recibí: " + msg["text"]["body"])
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
