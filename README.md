# Bot de WhatsApp

Bot en Flask que recibe mensajes por la API de WhatsApp Cloud (Meta) y responde.
Por ahora responde "eco" para probar la conexión.

## Deploy en Render
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Variables de entorno: `VERIFY_TOKEN`, `WA_TOKEN`, `PHONE_NUMBER_ID`

## Webhook en Meta
- URL: `https://TU-SERVICIO.onrender.com/webhook`
- Token de verificación: el mismo valor que `VERIFY_TOKEN`
- Suscribirse al campo `messages`
