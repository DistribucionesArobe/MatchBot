# MatchBot API

Backend de reservas deportivas vía WhatsApp. FastAPI + PostgreSQL en Render.

## Setup local

```bash
# 1. Clonar e instalar
pip install -r requirements.txt

# 2. Configurar variables
cp .env.example .env
# Edita .env con tu DATABASE_URL y SECRET_KEY

# 3. Crear tablas + seed
python seed.py

# 4. Correr
uvicorn main:app --reload
```

Docs en: http://localhost:8000/docs

---

## Deploy en Render

1. Crear nuevo **Web Service** apuntando al repo
2. Crear **PostgreSQL** database llamada `matchbot-db`
3. Conectar ambos (Render lo hace automático con `render.yaml`)
4. Agregar variables de entorno:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `SECRET_KEY` (Render puede generarlo automático)
5. Después del primer deploy, correr el seed:
   ```
   # En el shell de Render
   python seed.py
   ```

---

## Endpoints principales

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/api/auth/login` | Login admin |
| POST | `/api/auth/refresh` | Refresh JWT |
| GET | `/api/dashboard/metricas` | KPIs del día |
| GET | `/api/dashboard/ocupacion-heatmap` | Heatmap semanal |
| GET | `/api/dashboard/actividad-reciente` | Últimas reservas |
| GET | `/api/canchas` | Listar canchas |
| PATCH | `/api/canchas/:id/estado` | Toggle activa/cerrada |
| GET | `/api/reservas` | Listar con filtros |
| GET | `/api/reservas/disponibilidad` | Slots libres |
| POST | `/api/reservas` | Crear reserva manual |
| GET | `/api/clientes` | Listar clientes |
| PATCH | `/api/clientes/:id/bloqueo` | Bloquear/desbloquear |
| POST | `/webhook/whatsapp` | Webhook Twilio |

---

## Configurar Twilio Webhook

En tu consola Twilio, apunta el número de WhatsApp a:
```
https://api.matchbot.app/webhook/whatsapp
```
Método: POST

---

## Credenciales de prueba (después del seed)

```
Email:    admin@clubvictoria.mx
Password: Victoria2024!
```
