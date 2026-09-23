"""Carga la lista de precios y busca repuestos por código o por vehículo.

La lista se descarga desde LISTA_URL (Excel .xlsx o CSV) y se guarda en memoria.
Se vuelve a descargar sola cada LISTA_REFRESCO_MIN minutos, así los cambios
de precios en el archivo se reflejan sin tocar el código.

Columnas de la lista (por posición, empezando en la fila 4):
  A DT | B fecha stock verificado | C ubicación | D nro sistema | E códigos
  F descripción / aplicación | G comentarios internos | H costo base
  I precio taller / casa de repuestos | J precio particular | K categoría
  L subcategoría | M marca
Al cliente NUNCA se le muestran: ubicación, comentarios internos ni costo.
"""
import csv
import io
import math
import os
import re
import threading
import time
import unicodedata

import requests

LISTA_URL = os.environ.get("LISTA_URL", "")
REFRESCO_SEG = int(os.environ.get("LISTA_REFRESCO_MIN", "30")) * 60
FILA_INICIO = 4  # las filas 1-3 son encabezados

_productos = []
_indice_codigos = {}  # código normalizado -> set(índices): el producto ES ese código
_indice_menciones = {}  # código normalizado -> set(índices): el código aparece en la descripción
_ultima_carga = 0
_lock = threading.Lock()


# ---------- utilidades ----------

def _texto(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _numero(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def normalizar_codigo(c):
    """'0 445 110-183' -> '0445110183'"""
    return re.sub(r"[^0-9A-Z]", "", _texto(c).upper())


def _sin_acentos(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"(\d),(\d)", r"\1.\2", s)  # "1,9" -> "1.9"


def formatear_precio(p):
    return "$" + f"{p:,.0f}".replace(",", ".")


# ---------- carga ----------

def _leer_filas(contenido):
    if contenido[:2] == b"PK":  # es un .xlsx
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(min_row=FILA_INICIO, values_only=True)]
    texto = contenido.decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(texto)))[FILA_INICIO - 1:]


def _url_descarga(url):
    """Convierte links de Google Drive / Sheets en links de descarga directa."""
    m = re.search(r"/(?:file/d|spreadsheets/d)/([\w-]+)", url)
    if not m or "export" in url or "/pub" in url:
        return url
    if "/spreadsheets/d/" in url and "rtpof=true" not in url:
        # hoja nativa de Google Sheets
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    # archivo .xlsx subido a Drive
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"


def cargar(forzar=False, contenido=None):
    global _productos, _indice_codigos, _indice_menciones, _ultima_carga
    with _lock:
        if not forzar and contenido is None and time.time() - _ultima_carga < REFRESCO_SEG and _productos:
            return
        if contenido is None:
            r = requests.get(_url_descarga(LISTA_URL), timeout=60)
            r.raise_for_status()
            contenido = r.content
        filas = _leer_filas(contenido)

        productos, indice, menciones = [], {}, {}
        for f in filas:
            f = (list(f) + [None] * 13)[:13]
            codigos_txt, descripcion = _texto(f[4]), _texto(f[5])
            if not descripcion or not codigos_txt:
                continue
            if _texto(f[8]).lower().startswith("final"):  # fila de encabezado de sección
                continue
            codigos = [c for c in re.split(r"[\s/;,]+", codigos_txt) if c]
            nro_sistema = _texto(f[3])
            p = {
                "codigos": codigos,
                "nro_sistema": nro_sistema,
                "descripcion": descripcion,
                "precio_taller": _numero(f[8]),
                "precio_particular": _numero(f[9]),
                "categoria": _texto(f[10]),
                "subcategoria": _texto(f[11]),
                "marca": _texto(f[12]),
                "_busqueda": _sin_acentos(" ".join([descripcion, codigos_txt, _texto(f[11]), _texto(f[12])])),
                "_categoria": _sin_acentos(_texto(f[10])),
            }
            i = len(productos)
            productos.append(p)
            propios = set(normalizar_codigo(c) for c in codigos + [nro_sistema])
            for k in propios:
                if len(k) >= 3:
                    indice.setdefault(k, set()).add(i)
            # códigos que aparecen dentro de la descripción (OEM, equivalencias, "para 0445110183")
            for t in re.findall(r"\b[\dA-Z][\dA-Z\-\.]{5,}\b", descripcion.upper()):
                k = normalizar_codigo(t)
                if len(k) >= 6 and k not in propios and any(ch.isdigit() for ch in k):
                    menciones.setdefault(k, set()).add(i)

        _productos, _indice_codigos, _indice_menciones, _ultima_carga = productos, indice, menciones, time.time()
        print(f"Lista cargada: {len(productos)} productos", flush=True)


# ---------- búsquedas ----------

def _para_cliente(p, tipo_cliente):
    """Solo los datos que se le pueden mostrar al cliente, con el precio que le corresponde."""
    precio = p["precio_taller"] if tipo_cliente == "casa_de_repuestos" else p["precio_particular"]
    return {
        "codigos": " ".join(p["codigos"]),
        "descripcion": p["descripcion"],
        "marca": p["marca"],
        "categoria": p["categoria"],
        "precio": formatear_precio(precio) if precio > 0 else "a pedido / consultar precio",
    }


def _buscar_en(indice, q):
    idxs = list(indice.get(q, []))
    if not idxs and len(q) >= 5:
        # coincidencia parcial: "110183" encuentra "0445110183"
        for k, v in indice.items():
            if k.endswith(q) or (len(q) >= 7 and q in k):
                idxs.extend(v)
    return idxs


def buscar_por_codigo(codigo, tipo_cliente, limite=10):
    """Devuelve {'coincidencias': productos que SON ese código,
                 'relacionados': productos que lo mencionan (kits, piezas, alternativas)}"""
    cargar()
    q = normalizar_codigo(codigo)
    if len(q) < 3:
        return {"coincidencias": [], "relacionados": []}
    directos = list(dict.fromkeys(_buscar_en(_indice_codigos, q)))
    relacionados = [i for i in dict.fromkeys(_buscar_en(_indice_menciones, q)) if i not in directos]
    # si el código solo aparece en la descripción de un producto (ej. número OEM), ese producto es la coincidencia
    if not directos and len(relacionados) == 1:
        directos, relacionados = relacionados, []
    return {
        "coincidencias": [_para_cliente(_productos[i], tipo_cliente) for i in directos[:limite]],
        "relacionados": [_para_cliente(_productos[i], tipo_cliente) for i in relacionados[:limite]],
    }


def buscar_por_texto(consulta, tipo_cliente, limite=20):
    """Busca por vehículo, motor o tipo de pieza. Ej: 'inyector kangoo euro 4'."""
    cargar()
    # el año no se busca: en la lista aparece de formas muy distintas ("2012/2015", "a partir 2013")
    terminos = [t for t in re.split(r"\s+", _sin_acentos(consulta))
                if len(t) >= 2 and not re.fullmatch(r"(19|20)\d\d", t)]
    if not terminos:
        return []
    # Cada palabra pesa según lo rara que es en la lista: "sandero" pesa mucho, "2.0" o "inyector" poco.
    n = len(_productos)
    pesos = {}
    for t in terminos:
        df = sum(1 for p in _productos if t in p["_busqueda"])
        pesos[t] = math.log((n + 1) / (df + 1)) + 0.1
    puntaje = []
    for i, p in enumerate(_productos):
        # coincidir en la descripción vale el peso completo; solo en la categoría, la mitad
        s = sum(pesos[t] if t in p["_busqueda"] else pesos[t] * 0.5 if t in p["_categoria"] else 0
                for t in terminos)
        if s > 0:
            puntaje.append((s, i))
    puntaje.sort(key=lambda x: -x[0])
    mejor = puntaje[0][0] if puntaje else 0
    return [_para_cliente(_productos[i], tipo_cliente) for s, i in puntaje if s >= mejor * 0.6][:limite]
