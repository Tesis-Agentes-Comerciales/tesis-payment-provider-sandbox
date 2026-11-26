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

Con `PAYMENT_AP2_REQUIRED=true`, la delegación exige Payment Mandate SD-JWT del agente y Checkout
JWT del merchant, verifica ambas firmas y su coincidencia con el allowance antes de emitir token.
La confirmación genera un Payment Receipt firmado, recuperable por el agente autenticado mediante
`POST /agentic_commerce/payment_receipt`. Es un perfil Human Present sintético: no hay cargo real.

Human Not Present (perfil `ap2-hnp/v1`, en `docs/ap2-profile.md` del repo del agente): el agente
envía en `ap2_payment_mandate` la cadena de pago `<abierto>~~<cerrado>` y en el campo
`ap2_checkout_mandate` la cadena de checkout. El proveedor fija la raíz por el thumbprint de la JWK
del agente, verifica las dos cadenas (`aud`, `nonce`, `sd_hash`, `vct`, `iat`/`exp`), las
restricciones del abierto de pago (`amount_range`, `allowed_payees`,
`allowed_payment_instruments` y `reference` contra el abierto de checkout) y las mismas
comprobaciones de Human Present sobre el cerrado y el allowance. Un rechazo responde 422
`{"code", "payment_receipt_jwt"}` con un recibo de error AP2 firmado (`invalid_credential`,
`invalid_mandate` o `unresolved_constraint`); sin AP2 configurado, una cadena recibe 503
`mandates_not_supported` sin recibo. La idempotencia se consulta antes de verificar, igual que en
Human Present.

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

El [procedimiento CI-PSP-01](docs/testing.md) añade build, versiones fijadas, la ejecución
completa con JUnit individual y las guardas contra omisiones. GitHub Actions conserva
resultados y hashes; una verificación local no cierra la validación compartida.

Este servicio implementa un subconjunto de Delegate Payment de ACP `2026-04-17` para el
experimento, no conformidad ACP integral. No hay cargos, liquidación, reembolsos, disputas ni
3DS. El merchant mantiene la responsabilidad del checkout, inventario y orden.
