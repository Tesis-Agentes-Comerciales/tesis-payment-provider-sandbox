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
