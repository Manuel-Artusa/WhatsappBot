"""Manda consultas y contactos a la planilla de Google Sheets (ver google_apps_script.js).

Todo se envía en segundo plano: si la planilla tarda o falla, el cliente no se entera.
Si REGISTRO_URL no está configurada, no hace nada.
"""
import os
import threading

import requests

REGISTRO_URL = os.environ.get("REGISTRO_URL", "")
REGISTRO_CLAVE = os.environ.get("REGISTRO_CLAVE", "")

_http = requests.Session()
_http.trust_env = False

if REGISTRO_URL:
    print(f"Registro en planilla: ACTIVADO ({REGISTRO_URL[:60]}...)", flush=True)
else:
    print("Registro en planilla: DESACTIVADO (falta la variable REGISTRO_URL en Render)", flush=True)


def _enviar(datos):
    try:
        r = _http.post(REGISTRO_URL, json={**datos, "clave": REGISTRO_CLAVE}, timeout=30)
        if r.status_code != 200 or '"ok":true' not in r.text.replace(" ", ""):
            print("Registro en planilla falló:", r.status_code, r.text[:200], flush=True)
        else:
            print(f"Guardado en planilla: {datos.get('accion')}", flush=True)
    except Exception as e:
        print("No se pudo registrar en la planilla:", repr(e)[:200], flush=True)


def _en_segundo_plano(datos):
    if REGISTRO_URL:
        threading.Thread(target=_enviar, args=(datos,), daemon=True).start()


def pais_de(numero):
    if numero.startswith("595"):
        return "Paraguay"
    if numero.startswith("54"):
        return "Argentina"
    return ""


def contacto(numero, nombre="", tipo="", negocio="", nuevo_mensaje=False):
    _en_segundo_plano({"accion": "contacto", "numero": numero, "nombre": nombre or "",
                       "tipo": tipo or "", "negocio": negocio or "", "pais": pais_de(numero),
                       "nuevo_mensaje": nuevo_mensaje})


def consulta(numero, sesion, datos):
    _en_segundo_plano({"accion": "consulta", "numero": numero, "nombre": sesion.get("nombre") or "",
                       "tipo": sesion.get("tipo") or "", "negocio": sesion.get("negocio") or "", **datos})
