"""Circuito de pedidos entre el cliente, el bot y el vendedor.

  1. El cliente confirma el pedido  -> el bot se lo manda al vendedor para que revise stock.
  2. El vendedor responde           -> "ok" / "no hay stock de X" (en sus palabras, lo entiende Claude).
  3. El vendedor manda la factura   -> el bot se la reenvía al cliente junto con las cuentas para transferir.
  4. El cliente manda el comprobante-> el bot se lo reenvía al vendedor con la ubicación y cantidad de cada producto.
  5. El vendedor avisa que se entregó -> el pedido se cierra.

Los pedidos se guardan en la planilla (pestaña Pedidos), así no se pierden si el servidor se reinicia.
"""
import base64
import copy
import json
import os
import random
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

import catalogo
import registro
import whatsapp

ZONA = ZoneInfo("America/Argentina/Cordoba")
MODELO = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
# Números de los vendedores (separados por coma). Formato: 549 + característica + número.
VENDEDORES = [n.strip() for n in os.environ.get("VENDEDORES", "5493512047539").split(",") if n.strip()]

ESTADOS = {
    "esperando_stock": "el vendedor está revisando el stock",
    "falta_stock": "falta stock de algo: hay que ver con el cliente qué hace",
    "esperando_factura": "hay stock, el vendedor está armando la factura",
    "esperando_pago": "se mandó la factura y las cuentas, esperamos el comprobante de transferencia",
    "pago_informado": "el cliente mandó el comprobante, el vendedor verifica y prepara el pedido",
    "entregado": "entregado / despachado",
    "cancelado": "cancelado",
}
ABIERTOS = ("esperando_stock", "falta_stock", "esperando_factura", "esperando_pago", "pago_informado")

cliente_ia = anthropic.Anthropic(timeout=45, max_retries=1)
_pedidos = {}
_cargados = False
_lock = threading.RLock()
_factura_sin_asignar = {}   # vendedor -> (datos, mime, nombre_archivo) cuando no sabemos de qué pedido es
_historial_vendedor = {}    # vendedor -> últimos mensajes con el bot
_derivadas = []             # consultas que el bot le pasó al vendedor (no son pedidos)

# El bot conecta esta función para que lo que le decimos al cliente quede en su charla
al_avisar_cliente = None


def _leer_datos_pago():
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos_pago.txt")
    try:
        with open(ruta, encoding="utf-8") as f:
            return "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("#")).strip()
    except FileNotFoundError:
        return ""


DATOS_PAGO = _leer_datos_pago()


# ---------- utilidades ----------

def es_vendedor(numero):
    return any(whatsapp.mismo_numero(numero, v) for v in VENDEDORES)


def _ahora():
    return datetime.now(ZONA).strftime("%d/%m/%Y %H:%M")


def _precio_a_numero(p):
    if isinstance(p, (int, float)):
        return float(p)
    digitos = re.sub(r"[^\d,]", "", str(p or "")).replace(",", ".")
    try:
        return float(digitos)
    except ValueError:
        return 0.0


def _asegurar_cargados():
    global _cargados
    with _lock:
        if _cargados:
            return
        _cargados = True
        for p in registro.pedidos_abiertos():
            if p.get("id"):
                _pedidos[p["id"]] = p
        if _pedidos:
            print(f"Pedidos abiertos recuperados de la planilla: {len(_pedidos)}", flush=True)


def _guardar(p, estado=None):
    if estado:
        p["estado"] = estado
    p["actualizado"] = _ahora()
    p.setdefault("historia", []).append(f"{p['actualizado']} {p['estado']}")
    registro.guardar_pedido(copy.deepcopy(p))  # copia: se manda en segundo plano


def _nuevo_id():
    while True:
        pid = f"P-{random.randint(1000, 9999)}"
        if pid not in _pedidos:
            return pid


def _texto_items(p, para_vendedor=False):
    lineas = []
    for i in p["items"]:
        if para_vendedor and not (i.get("codigo_fact") and i.get("ubicacion")):
            extra = catalogo.datos_internos(i.get("codigo", ""), ref=i.get("ref"),
                                            precio=round(_precio_a_numero(i.get("precio_unitario"))) or None,
                                            descripcion=i.get("descripcion", ""))
            i["codigo_fact"] = i.get("codigo_fact") or extra["codigo_fact"]
            i["ubicacion"] = i.get("ubicacion") or extra["ubicacion"]
        precio = (f" — {catalogo.formatear_precio(_precio_a_numero(i['precio_unitario']))} c/u"
                  if i.get("precio_unitario") else "")
        if para_vendedor:
            # en negrita, salvo que el dato ya tenga asteriscos (ej. ubicación "*i3") y se rompa el formato
            neg = lambda v: f"*{v}*" if "*" not in v else v  # noqa: E731
            lineas.append(f"• *{i.get('cantidad', 1)}x* — Cód. facturación: {neg(i.get('codigo_fact') or 'sin dato')}{precio}\n"
                          f"   {i.get('codigo', '')} {i.get('descripcion', '')}\n"
                          f"   📍 Ubicación: {neg(i.get('ubicacion') or 'sin dato')}")
        else:
            lineas.append(f"• {i.get('cantidad', 1)}x *{i.get('codigo', '')}* {i.get('descripcion', '')}{precio}")
    return "\n".join(lineas)


def _cliente_txt(p):
    quien = p.get("nombre") or "Cliente"
    if p.get("tipo") == "casa_de_repuestos":
        quien += f" (casa de repuestos: {p.get('negocio') or '-'})"
    return f"{quien} — +{p['numero']}"


def _avisar_vendedores(texto):
    for v in VENDEDORES:
        whatsapp.enviar(v, texto)


def _avisar_cliente(p, texto):
    whatsapp.enviar(p["numero"], texto)
    if al_avisar_cliente:
        al_avisar_cliente(p["numero"], texto)


def pedidos_de(numero, solo_abiertos=True):
    _asegurar_cargados()
    with _lock:
        ps = [p for p in _pedidos.values() if whatsapp.mismo_numero(p["numero"], numero)
              and (not solo_abiertos or p["estado"] in ABIERTOS)]
    return sorted(ps, key=lambda p: p.get("fecha", ""), reverse=True)


def resumen_para_cliente(numero):
    """Para el bot del cliente: en qué está su pedido (va en las instrucciones de Claude)."""
    ps = pedidos_de(numero)
    if not ps:
        return "El cliente no tiene pedidos en curso."
    return "\n".join(f"- Pedido {p['id']} ({p['estado']}: {ESTADOS[p['estado']]}). Productos:\n{_texto_items(p)}"
                     for p in ps)


# ---------- 1. el cliente confirma el pedido ----------

def crear(numero, sesion, datos):
    _asegurar_cargados()
    items = []
    for i in datos.get("items", []):
        i = dict(i)
        i["cantidad"] = int(_precio_a_numero(i.get("cantidad", 1)) or 1)
        # código de facturación y ubicación del producto exacto que eligió
        i.update(catalogo.datos_internos(i.get("codigo", ""), ref=i.get("ref"),
                                         precio=round(_precio_a_numero(i.get("precio_unitario"))) or None,
                                         descripcion=i.get("descripcion", "")))
        items.append(i)
    if not items:
        return {"error": "El pedido no tiene productos."}
    total = sum(_precio_a_numero(i.get("precio_unitario")) * i["cantidad"] for i in items)

    with _lock:
        # Si el cliente ya tenía un pedido sin confirmar (esperando stock o con faltante),
        # este lo reemplaza con el mismo número: es el mismo pedido, corregido.
        previo = next((x for x in pedidos_de(numero) if x["estado"] in ("esperando_stock", "falta_stock")), None)
        p = previo or {"id": _nuevo_id(), "fecha": _ahora(), "numero": numero}
        p.update({
            "estado": "esperando_stock",
            "nombre": sesion.get("nombre") or p.get("nombre", ""), "tipo": sesion.get("tipo") or "",
            "negocio": sesion.get("negocio") or "", "items": items,
            "total": catalogo.formatear_precio(total) if total else "a confirmar",
            "datos_factura": datos.get("datos_factura", ""), "envio": datos.get("envio", ""),
            "notas": datos.get("notas", ""),
        })
        _pedidos[p["id"]] = p
        _guardar(p)

    _avisar_vendedores(
        (f"✏️ *PEDIDO {p['id']} ACTUALIZADO* (reemplaza al anterior)\n" if previo else f"🧾 *NUEVO PEDIDO {p['id']}*\n")
        + f"Cliente: {_cliente_txt(p)}\n\n"
        f"{_texto_items(p, para_vendedor=True)}\n\n"
        f"*Total:* {p['total']}\n"
        f"*Factura:* {p['datos_factura'] or '-'}\n"
        f"*Entrega:* {p['envio'] or '-'}\n"
        + (f"*Notas:* {p['notas']}\n" if p["notas"] else "")
        + f"\n👉 Revisá el stock y respondeme, por ejemplo:\n"
        f"*{p['id']} ok*  o  *{p['id']} no hay stock de ...*\n"
        f"Después mandame la factura (PDF o foto) y se la paso al cliente."
    )
    print(f"[{numero}] Pedido {p['id']} creado y enviado al vendedor", flush=True)
    return {"ok": True, "pedido": p["id"], "total": p["total"],
            "nota": "Decile al cliente que un vendedor está revisando el stock y que apenas esté le mandás la factura y los datos para transferir. No le pases las cuentas todavía."}


def derivar_consulta(numero, sesion, resumen):
    """Consultas que no son un pedido (precio a confirmar, algo que el bot no sabe, etc.)."""
    _derivadas.append({"numero": numero, "nombre": sesion.get("nombre") or "", "resumen": resumen, "fecha": _ahora()})
    del _derivadas[:-10]
    _avisar_vendedores(
        f"❓ *CONSULTA PARA RESPONDER*\nCliente: {sesion.get('nombre') or 'Cliente'} — +{numero}\n\n{resumen}\n\n"
        f"👉 Podés contestarme acá y se lo paso, por ejemplo: *decile a +{numero} que ...*")


# ---------- 2 y 5. mensajes del vendedor ----------

HERRAMIENTAS_VENDEDOR = [
    {"name": "confirmar_stock", "description": "El vendedor confirma que hay stock de todo el pedido.",
     "input_schema": {"type": "object", "properties": {"pedido": {"type": "string"}}, "required": ["pedido"]}},
    {"name": "falta_stock", "description": "Falta stock de uno o más productos del pedido.",
     "input_schema": {"type": "object", "properties": {
         "pedido": {"type": "string"},
         "mensaje_para_cliente": {"type": "string", "description": "Qué falta y qué alternativa hay, escrito para el cliente, cordial y en rioplatense"}},
         "required": ["pedido", "mensaje_para_cliente"]}},
    {"name": "mensaje_a_cliente", "description": "Mandarle al cliente un mensaje que pide el vendedor.",
     "input_schema": {"type": "object", "properties": {
         "pedido_o_numero": {"type": "string", "description": "Número de pedido (P-1234) o teléfono del cliente"},
         "mensaje": {"type": "string", "description": "El mensaje, redactado para el cliente"}},
         "required": ["pedido_o_numero", "mensaje"]}},
    {"name": "buscar_en_lista", "description": "Busca un producto en la lista de precios con los datos internos: código de facturación (columna D, NRO SISTEMA), ubicación en el depósito y precios.",
     "input_schema": {"type": "object", "properties": {"codigo_o_texto": {"type": "string"}}, "required": ["codigo_o_texto"]}},
    {"name": "marcar_entregado", "description": "El pedido ya se entregó o despachó.",
     "input_schema": {"type": "object", "properties": {"pedido": {"type": "string"}}, "required": ["pedido"]}},
    {"name": "cancelar_pedido", "description": "Cancelar un pedido.",
     "input_schema": {"type": "object", "properties": {
         "pedido": {"type": "string"}, "mensaje_para_cliente": {"type": "string"}}, "required": ["pedido"]}},
]


def _buscar_pedido(texto):
    m = re.search(r"P-?\s?(\d{4})", str(texto or ""), re.IGNORECASE)
    return _pedidos.get(f"P-{m.group(1)}") if m else None


def _ejecutar_vendedor(nombre, d):
    if nombre == "buscar_en_lista":
        q = d["codigo_o_texto"]
        res = catalogo.buscar_por_codigo(q, "casa_de_repuestos")
        filas = res["coincidencias"] or catalogo.buscar_por_texto(q, "casa_de_repuestos", limite=8)
        salida = []
        for f in filas[:8]:
            codigo = f["codigos"].split()[0] if f["codigos"] else ""
            internos = catalogo.datos_internos(codigo)
            precios = catalogo.buscar_por_codigo(codigo, "particular")["coincidencias"]
            salida.append({**f, "precio_casa_de_repuestos": f["precio"],
                           "precio_particular": precios[0]["precio"] if precios else "",
                           "codigo_facturacion": internos["codigo_fact"], "ubicacion": internos["ubicacion"]})
        return {"resultados": salida} if salida else {"resultados": [], "nota": "No aparece en la lista."}
    if nombre == "mensaje_a_cliente":
        p = _buscar_pedido(d["pedido_o_numero"])
        numero = p["numero"] if p else re.sub(r"\D", "", d["pedido_o_numero"])
        if not numero:
            return {"error": "No sé a qué cliente mandárselo."}
        whatsapp.enviar(numero, d["mensaje"])
        if al_avisar_cliente:
            al_avisar_cliente(numero, d["mensaje"])
        return {"ok": True}
    p = _buscar_pedido(d.get("pedido"))
    if not p:
        return {"error": f"No encuentro el pedido {d.get('pedido')}."}
    if nombre == "confirmar_stock":
        _guardar(p, "esperando_factura")
        _avisar_cliente(p, "¡Buenas noticias! Tenemos todo lo de tu pedido. En un ratito te mando la factura y los datos para transferir.")
        return {"ok": True, "nota": "Pedile al vendedor que te mande la factura (PDF o foto)."}
    if nombre == "falta_stock":
        _guardar(p, "falta_stock")
        _avisar_cliente(p, d["mensaje_para_cliente"])
        return {"ok": True}
    if nombre == "marcar_entregado":
        _guardar(p, "entregado")
        return {"ok": True}
    if nombre == "cancelar_pedido":
        _guardar(p, "cancelado")
        if d.get("mensaje_para_cliente"):
            _avisar_cliente(p, d["mensaje_para_cliente"])
        return {"ok": True}
    return {"error": "acción desconocida"}


def mensaje_vendedor(numero, texto="", archivo=None):
    """Procesa lo que escribe o manda un vendedor. Devuelve la respuesta para el vendedor.
    archivo: (datos, mime, nombre_archivo) si mandó una factura."""
    _asegurar_cargados()

    # 3. una factura (PDF o foto)
    if archivo:
        return _recibir_factura(numero, archivo, texto)
    if numero in _factura_sin_asignar and _buscar_pedido(texto):
        return _recibir_factura(numero, _factura_sin_asignar.pop(numero), texto)

    with _lock:
        abiertos = [p for p in _pedidos.values() if p["estado"] in ABIERTOS]
    lista = "\n\n".join(
        f"PEDIDO {p['id']} — estado: {p['estado']} ({ESTADOS[p['estado']]})\nCliente: {_cliente_txt(p)}\n"
        f"{_texto_items(p, para_vendedor=True)}\nTotal: {p['total']} | Factura: {p.get('datos_factura') or '-'} | "
        f"Entrega: {p.get('envio') or '-'}"
        for p in abiertos) or "(no hay pedidos abiertos)"
    derivadas = "\n".join(f"- {c['fecha']} +{c['numero']} {c['nombre']}: {c['resumen'][:150]}" for c in _derivadas) or "(ninguna)"
    sistema = f"""Sos el asistente interno de ventas de una casa de repuestos. Te escribe un VENDEDOR (no un cliente).
Tu trabajo: entender qué quiere hacer con los pedidos y usar las herramientas. Respondele corto y claro, en rioplatense.

PEDIDOS ABIERTOS:
{lista}

CONSULTAS DERIVADAS RECIENTES:
{derivadas}

- "ok", "hay todo", "dale" sobre un pedido -> confirmar_stock. Si hay un solo pedido esperando stock y no dice cuál, es ese.
- Si dice que falta algo -> falta_stock, con un mensaje para el cliente que explique qué falta y la alternativa si la dio.
- Si quiere que le digas algo a un cliente -> mensaje_a_cliente.
- "entregado", "retiró", "despachado" -> marcar_entregado.
- Si no queda claro de qué pedido habla y hay varios, preguntale.
- La factura la manda como archivo (PDF o foto): si pregunta, explicale eso.
- Tenés TODOS los datos internos: código de facturación (columna D de la lista, "NRO SISTEMA"), ubicación en el depósito,
  cantidades, precios y datos de factura. Están arriba en cada pedido y podés buscar cualquier producto con buscar_en_lista.
  Nunca digas que no tenés acceso a la lista o al Excel.
- Cuando te pida los datos de un pedido, pasale por cada producto: cantidad, código de facturación, código, descripción y ubicación."""
    hist = _historial_vendedor.setdefault(numero, [])
    mensajes = hist[-8:] + [{"role": "user", "content": texto or "(mensaje vacío)"}]
    textos = []
    for _ in range(5):
        r = cliente_ia.messages.create(model=MODELO, max_tokens=800, system=sistema,
                                       tools=HERRAMIENTAS_VENDEDOR, messages=mensajes)
        textos += [b.text for b in r.content if b.type == "text" and b.text.strip()]
        mensajes.append({"role": "assistant", "content": r.content})
        if r.stop_reason != "tool_use":
            break
        res = []
        for b in r.content:
            if b.type == "tool_use":
                print(f"[vendedor] {b.name}({b.input})", flush=True)
                try:
                    salida = _ejecutar_vendedor(b.name, b.input)
                except Exception as e:
                    salida = {"error": repr(e)[:200]}
                res.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(salida, ensure_ascii=False)})
        mensajes.append({"role": "user", "content": res})
    respuesta = "\n\n".join(textos).strip() or "Listo 👍"
    hist += [{"role": "user", "content": texto or "(mensaje vacío)"}, {"role": "assistant", "content": respuesta}]
    del hist[:-12]
    return respuesta


# ---------- 3. la factura ----------

def _recibir_factura(vendedor, archivo, texto):
    p = _buscar_pedido(texto)
    if not p:
        with _lock:
            candidatos = [x for x in _pedidos.values() if x["estado"] == "esperando_factura"] or \
                         [x for x in _pedidos.values() if x["estado"] == "esperando_stock"]
        if len(candidatos) == 1:
            p = candidatos[0]
        else:
            _factura_sin_asignar[vendedor] = archivo
            if not candidatos:
                return "Recibí el archivo, pero no tengo pedidos esperando factura. Si es de un pedido, decime el número (ej: P-1234)."
            return "¿De qué pedido es esta factura? Respondeme con el número: " + ", ".join(
                f"*{x['id']}* ({x.get('nombre') or x['numero']})" for x in candidatos)

    datos, mime, nombre_archivo = archivo
    extension = "pdf" if mime == "application/pdf" else (mime.split("/")[-1] if "/" in mime else "pdf")
    whatsapp.enviar_archivo(p["numero"], datos, mime, f"Factura {p['id']}.{extension}",
                            f"Tu factura del pedido {p['id']}")
    mensaje_pago = (
        f"Te paso la factura de tu pedido 👆 Total: *{p['total']}*\n\n"
        f"Podés transferir a cualquiera de estas cuentas:\n\n{DATOS_PAGO}\n\n"
        f"Cuando hagas la transferencia, mandame por acá el *comprobante* y te preparamos el pedido.")
    _avisar_cliente(p, mensaje_pago)
    _guardar(p, "esperando_pago")
    print(f"[vendedor] Factura del pedido {p['id']} enviada al cliente", flush=True)
    return f"✅ Le mandé la factura y los datos de pago a {_cliente_txt(p)} (pedido {p['id']}). Te aviso cuando mande el comprobante."


# ---------- 4. el comprobante del cliente ----------

HERRAMIENTA_COMPROBANTE = {
    "name": "resultado", "description": "Resultado del análisis del archivo.",
    "input_schema": {"type": "object", "properties": {
        "es_comprobante": {"type": "boolean", "description": "true si es un comprobante de transferencia o pago"},
        "monto": {"type": "string"}, "fecha": {"type": "string"},
        "destinatario": {"type": "string", "description": "A quién o a qué alias/CBU se transfirió"},
        "observaciones": {"type": "string", "description": "Algo raro: monto distinto, otra cuenta, ilegible, etc."}},
        "required": ["es_comprobante"]}}


PALABRAS_PAGO = re.compile(r"transf|comprobante|pag(u|o|a|é|ue)|deposit|abon", re.I)


def _bloque_archivo(datos, mime):
    fuente = {"type": "base64", "media_type": mime, "data": base64.standard_b64encode(datos).decode()}
    if mime == "application/pdf":
        return {"type": "document", "source": fuente}
    if mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return {"type": "image", "source": fuente}
    return None


def revisar_comprobante(numero, datos, mime, nombre_archivo, texto=""):
    """Se llama con cada foto o PDF que manda un cliente. Si es un comprobante de pago, se lo reenvía
    al vendedor (con el pedido si lo encuentra) y devuelve la respuesta para el cliente.
    Si no es un comprobante, devuelve None y la charla sigue normal."""
    bloque = _bloque_archivo(datos, mime)
    if not bloque:
        return None
    abiertos = pedidos_de(numero)
    # Solo gastamos en revisar si puede ser un pago: hay un pedido abierto, es un PDF o el texto habla de pago
    if not (abiertos or mime == "application/pdf" or PALABRAS_PAGO.search(texto or "")):
        return None
    p = next((x for x in abiertos if x["estado"] == "esperando_pago"), abiertos[0] if abiertos else None)
    try:
        r = cliente_ia.messages.create(
            model=MODELO, max_tokens=500, tools=[HERRAMIENTA_COMPROBANTE], tool_choice={"type": "tool", "name": "resultado"},
            messages=[{"role": "user", "content": [bloque, {"type": "text", "text":
                "¿Esto es un comprobante de transferencia o de pago? "
                + (f"El total del pedido es {p['total']}. " if p else "")
                + f"Las cuentas válidas para transferir son:\n{DATOS_PAGO}\nExtraé los datos."}]}])
        info = next(b.input for b in r.content if b.type == "tool_use")
    except Exception as e:
        print("No se pudo analizar el archivo:", repr(e)[:200], flush=True)
        info = {"es_comprobante": bool(p and p["estado"] == "esperando_pago"),
                "observaciones": "no se pudo leer automáticamente"}
    print(f"[{numero}] ¿Es comprobante? {info.get('es_comprobante')} — monto {info.get('monto')}", flush=True)
    if not info.get("es_comprobante"):
        return None
    return reenviar_comprobante(numero, datos, mime, nombre_archivo, info, p)


def reenviar_comprobante(numero, datos, mime, nombre_archivo, info, p=None, sesion=None):
    """Le pasa el comprobante al vendedor con el pedido para preparar. Si no encuentra el pedido
    (por ejemplo, se reinició el servidor), igual se lo pasa para que lo revise a mano."""
    if p is None:
        abiertos = pedidos_de(numero)
        p = next((x for x in abiertos if x["estado"] == "esperando_pago"), abiertos[0] if abiertos else None)
    titulo = f"pedido {p['id']}" if p else "pago de cliente"
    for v in VENDEDORES:
        try:
            whatsapp.enviar_archivo(v, datos, mime, nombre_archivo or "comprobante.jpg", f"Comprobante — {titulo}")
        except Exception as e:
            print("No se pudo reenviar el comprobante:", repr(e)[:200], flush=True)
    datos_pago = (f"Monto del comprobante: *{info.get('monto') or '?'}*"
                  + (f" (total del pedido: {p['total']})" if p else "") + "\n"
                  f"Destino: {info.get('destinatario') or '?'} — Fecha: {info.get('fecha') or '?'}\n"
                  + (f"⚠️ {info['observaciones']}\n" if info.get("observaciones") else "")
                  + "*Verificá que la plata esté acreditada antes de entregar.*\n\n")
    if p:
        _avisar_vendedores(
            f"💰 *PAGO INFORMADO — PEDIDO {p['id']}*\nCliente: {_cliente_txt(p)}\n" + datos_pago
            + f"📦 *PREPARAR:*\n{_texto_items(p, para_vendedor=True)}\n\n"
            f"*Entrega:* {p['envio'] or '-'}\n\n"
            f"Cuando se entregue o despache, avisame: *{p['id']} entregado*")
        _guardar(p, "pago_informado")
        print(f"[{numero}] Comprobante del pedido {p['id']} reenviado al vendedor", flush=True)
    else:
        nombre = (sesion or {}).get("nombre") or "Cliente"
        _avisar_vendedores(
            f"💰 *COMPROBANTE RECIBIDO*\nCliente: {nombre} — +{numero}\n" + datos_pago
            + "No encontré un pedido abierto de este cliente (puede que se haya reiniciado el sistema). "
              "Revisá la charla para ver qué compró.")
        print(f"[{numero}] Comprobante sin pedido asociado: reenviado igual al vendedor", flush=True)
    return "¡Recibí el comprobante, gracias! 🙌 Apenas se acredite te preparamos el pedido y te avisamos."
