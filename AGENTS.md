# Proveedor de pagos simulado

Este repositorio es el proveedor de pagos simulado e independiente de la tesis. Python 3.12 y `uv`;
use `uv sync`, no `pip`, y no edite `uv.lock` a mano. Antes de entregar cambios, `scripts/check.sh`
debe pasar en verde: es la compuerta local (lock, formato, lint, tipos y pruebas).

Sin tarjetas reales, CVC, cargos, liquidación ni reembolsos. El proveedor es dueño de la
delegación, el alcance y el vencimiento del token, la confirmación de un solo uso y el resultado de
las tarjetas de prueba. Los tokens son opacos para el agente y el comercio: ninguno puede deducir
del texto del token si el pago se aprueba. No guarde tarjetas, Bearer ni tokens en claro en SQLite,
logs o archivos versionados. Mantenga separadas la autenticación del agente y la del comercio, y
los POST con efecto idempotentes.

`uv run demo-stack`, en el repositorio del agente, arranca este servicio con
`uvicorn tesis_payment_provider.api:app_factory --factory` en el puerto 4130 y le pasa sus
variables `PAYMENT_*`; no cambie ese punto de entrada ni esas variables.
