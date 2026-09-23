# Tesis Payment Provider Sandbox

Proveedor sintético independiente para la tesis. Es un tercer servicio y repositorio: el agente
le solicita un token delegado; el merchant le confirma el monto autoritativo antes de crear una
orden. No procesa dinero, tarjetas reales ni CVC.

## Fronteras

1. `POST /agentic_commerce/delegate_payment`: solo el agente, con su Bearer. Recibe una tarjeta
   **de prueba** y un allowance de un checkout, merchant, moneda, importe máximo y expiración.
   Emite un token opaco `vt_*`; el resultado no está codificado en el token.
2. `POST /payments/confirm`: solo el merchant, con otro Bearer. Verifica alcance, vencimiento,
   uso único e idempotencia. Devuelve `approved` o `declined` según el escenario de la tarjeta
   de prueba. Un token desconocido o fuera de alcance no aprueba un pago.
3. `POST /payments/lookup`: solo el merchant, para consultar un token sin consumirlo.

El estado vive en SQLite propia del proveedor. Guarda hashes de tokens y claves idempotentes;
no guarda el número de tarjeta ni el token en claro. Las tarjetas sintéticas soportadas son
`4242424242424242` (aprobada) y `4000000000000002` (rechazada). No use PAN reales.

## Arranque

```sh
uv sync --frozen --all-groups
cp -n .env.example .env
uv run uvicorn tesis_payment_provider.api:app_factory --factory --host 127.0.0.1 --port 4130
```

Configure tres secretos distintos de al menos 32 caracteres en `.env`. Comparta solo
`PAYMENT_AGENT_BEARER_TOKEN` con el agente y solo `PAYMENT_MERCHANT_BEARER_TOKEN` con el merchant.
El reloj fijo `PAYMENT_SANDBOX_CLOCK_AT` existe únicamente para los episodios sintéticos cuyo
checkout también usa fechas fijas; no habilita un proveedor de producción.

```sh
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

Este servicio implementa un subconjunto de Delegate Payment de ACP `2026-04-17` para el
experimento, no conformidad ACP integral. No hay cargos, liquidación, reembolsos, disputas ni
3DS. El merchant mantiene la responsabilidad del checkout, inventario y orden.

## Despliegue sandbox

El `Dockerfile` sirve la API en `0.0.0.0:${PORT:-8080}`. En Railway, configure
`PAYMENT_DATABASE_PATH=/app/.local/payments.db` y monte un volumen persistente en `/app/.local`;
sin volumen, los tokens e idempotencias se perderían al reiniciar. Configure los tres secretos de
`.env.example` como variables del servicio, con el mismo token de agente y merchant que usarán los
otros dos servicios. `PAYMENT_MERCHANT_ID` debe coincidir con el del merchant y el agente.

El catálogo de demostración usa un reloj sintético fijo. Para probar ese episodio, configure
`PAYMENT_SANDBOX_CLOCK_AT=2026-09-10T14:05:00Z` junto con
`AGENT_SANDBOX_CLOCK_AT=2026-09-10T14:05:00Z` en el agente. Esta opción no corresponde a pagos
reales. `GET /health` verifica que el servicio inició; después pruebe el flujo completo antes de
exponer la UI.
