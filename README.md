# Bot de WhatsApp para repuestos

Atiende por WhatsApp como un vendedor: pregunta si es casa de repuestos o particular,
pide el código, lo busca en la lista de precios (o busca equivalencias en Google),
o busca por vehículo si no tiene el código. Toma datos de factura y envío y avisa a un vendedor.

## Archivos
- `app.py`: webhook de WhatsApp (recibe y envía mensajes)
- `bot.py`: la conversación con Claude (instrucciones del vendedor y herramientas)
- `catalogo.py`: carga la lista de precios y hace las búsquedas

## Variables de entorno (Render → Environment)
| Variable | Qué es |
|---|---|
| `VERIFY_TOKEN` | palabra para verificar el webhook en Meta |
| `WA_TOKEN` | token de acceso de WhatsApp |
| `PHONE_NUMBER_ID` | Phone Number ID del número |
| `ANTHROPIC_API_KEY` | clave de la API de Claude (console.anthropic.com) |
| `LISTA_URL` | link de la lista de precios en Google Drive / Sheets |
| `NOMBRE_NEGOCIO` | (opcional) nombre con el que se presenta el bot |
| `CLAUDE_MODEL` | (opcional) modelo, por defecto `claude-sonnet-4-5` |
| `LISTA_REFRESCO_MIN` | (opcional) cada cuántos minutos relee la lista, por defecto 30 |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | (opcional) para recibir avisos de pedidos por Telegram |
| `TRANSCRIPCION_API_KEY` | (opcional) clave de Groq (console.groq.com) para entender audios |
| `TRANSCRIPCION_URL`, `TRANSCRIPCION_MODELO` | (opcional) para usar otro servicio de transcripción compatible con OpenAI |
| `REGISTRO_URL`, `REGISTRO_CLAVE` | (opcional) planilla de Google Sheets donde se guardan consultas y contactos (ver `google_apps_script.js`) |
| `VENDEDORES` | número(s) de WhatsApp de los vendedores, separados por coma (ej: `5493512047539`) |
| `PLANTILLA_AVISO`, `PLANTILLA_IDIOMA` | (opcional) plantilla aprobada de WhatsApp para avisar a alguien que no escribió en las últimas 24 h |
| `BUSQUEDA_WEB` | (opcional) `0` para que no busque equivalencias en Google |

## Deploy en Render
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`

## Pedidos (pedidos.py)
1. El cliente confirma → el bot le manda el pedido al vendedor (con la ubicación de cada producto).
2. El vendedor responde "P-1234 ok" o "P-1234 no hay stock de ..." (en sus palabras).
3. El vendedor manda la factura (PDF o foto) → el bot se la reenvía al cliente con las cuentas de `datos_pago.txt`.
4. El cliente manda el comprobante → el bot se lo reenvía al vendedor con qué preparar y dónde está.
5. El vendedor avisa "P-1234 entregado" → se cierra.
Todo queda en la pestaña Pedidos de la planilla.
