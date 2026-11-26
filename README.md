# Tesis Payment Provider Sandbox

Proveedor de pagos simulado de la tesis. Le entrega al agente comprador un token de pago después
de verificar los mandatos de AP2 (Agent Payments Protocol) y le confirma el pago al comercio. No
mueve dinero: solo acepta tarjetas de prueba y no hace cargos.

Es uno de tres servicios separados, cada uno con su repositorio y su base de datos: el agente, el
comercio y este proveedor.

## Qué hace

| Ruta | Quién la llama | Qué hace |
| --- | --- | --- |
| `POST /agentic_commerce/delegate_payment` | Agente | Recibe una tarjeta de prueba y el límite del pago (checkout, comercio, moneda, monto máximo y vencimiento), verifica los mandatos y entrega un token opaco de un solo uso. |
| `POST /payments/confirm` | Comercio | Consume el token si el cobro cabe en ese límite y responde `approved` o `declined`. |
| `POST /payments/lookup` | Comercio | Consulta un token sin consumirlo. |
| `POST /agentic_commerce/payment_receipt` | Agente | Devuelve el recibo de pago firmado. |
| `GET /.well-known/ap2/jwks.json` | Cualquiera | Publica la llave con la que se verifican los recibos. |
| `GET /health` | Cualquiera | Responde `{"status": "ok"}`. |

El agente y el comercio tienen cada uno su propio Bearer, y ninguno puede usar las rutas del otro.

Con `PAYMENT_AP2_REQUIRED=true`, que es como lo arranca la demo, el proveedor no entrega un token
sin verificar antes el mandato de pago que firma el agente y el checkout que firma el comercio, y
que ambos coincidan con el límite del pago. En las compras delegadas, cuando el comprador no está
presente, verifica además la cadena que empieza en el mandato abierto, emitido cuando el comprador
dio su consentimiento por adelantado, y sus límites de monto, comercios e instrumentos de pago. Un
rechazo responde 422 con un recibo de error firmado. El perfil completo está en
[`docs/ap2-profile.md`](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/blob/main/docs/ap2-profile.md)
del agente.

Su base SQLite guarda hashes de los tokens y de las claves de idempotencia, nunca el número de
tarjeta ni el token en claro.

## Cómo correrlo

Lo normal es no arrancarlo a mano. Se clonan los tres repositorios lado a lado, con estos nombres
exactos:

```sh
git clone https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent.git
git clone https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant.git
git clone https://github.com/Tesis-Agentes-Comerciales/tesis-payment-provider-sandbox.git
```

Desde `tesis-commerce-agent`, `uv run demo-stack` levanta el comercio, este proveedor, el agente y
la interfaz. Genera secretos y llaves nuevos en cada arranque y se los pasa a cada servicio, así
que aquí no hace falta `.env`. Los requisitos y los pasos previos están en el
[README del agente](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent#readme).

## Arranque manual

Para correrlo por separado hacen falta Python 3.12 y `uv` (uv instala la versión exacta de
`.python-version` si falta):

```sh
uv sync --frozen --all-groups
cp -n .env.example .env
uv run uvicorn tesis_payment_provider.api:app_factory --factory --host 127.0.0.1 --port 4130
```

Antes de arrancar, `.env` necesita tres secretos distintos de al menos 32 caracteres; sirve, por
ejemplo, `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Solo
`PAYMENT_AGENT_BEARER_TOKEN` se comparte con el agente y solo `PAYMENT_MERCHANT_BEARER_TOKEN` con
el comercio, y `PAYMENT_MERCHANT_ID` debe coincidir con el del comercio. Para verificar mandatos
AP2 se activa `PAYMENT_AP2_REQUIRED=true` junto con las llaves que lista `.env.example`.
`curl http://127.0.0.1:4130/health` confirma que arrancó.

## Pruebas

```sh
scripts/check.sh
```

Revisa el lock, el formato, el lint y los tipos, y corre toda la suite; se detiene en el primer
fallo. No necesita `.env` ni los otros servicios. Qué cubre cada prueba y qué no, en
[docs/testing.md](docs/testing.md).

## Límites

- Implementa solo la parte de Delegate Payment de ACP (Agentic Commerce Protocol, versión
  `2026-04-17`) que usa el experimento, no el protocolo completo.
- No hace cargos, liquidación, reembolsos ni disputas, ni 3DS (la verificación adicional que pide
  el banco emisor de la tarjeta).
- Solo acepta dos tarjetas de prueba y no pide CVC: `4242424242424242` se aprueba y
  `4000000000000002` se rechaza. No se deben usar tarjetas reales.
- El comercio sigue siendo responsable del checkout, el inventario y la orden.
