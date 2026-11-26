# Pruebas

Desde la raíz del repositorio:

```sh
scripts/check.sh
```

El script se detiene en el primer fallo y corre, en orden:

1. `uv lock --check`: el lock corresponde a `pyproject.toml`.
2. `uv run ruff format --check .` y `uv run ruff check .`: formato y lint.
3. `uv run pyright`: tipos de `src/` y `tests/`.
4. `uv run pytest -q`: toda la suite de `tests/`.

Las pruebas arrancan el proveedor dentro del proceso, con una SQLite temporal y llaves generadas
en cada prueba. No leen `.env` ni necesitan el agente, el comercio o la red, salvo para descargar
las dependencias la primera vez.

## Controles de pagos existentes y límites

| Propiedad | Prueba que la cubre |
| --- | --- |
| Identidad del agente y del comercio | `test_wrong_actor_cannot_issue_or_confirm`: el Bearer del otro rol recibe 401. |
| Token opaco, de un solo uso, con tope de monto e idempotente | `test_token_is_opaque_scoped_single_use_and_idempotent`: un monto mayor recibe 422, repetir la misma petición devuelve la misma respuesta, confirmar con otra clave un token ya usado recibe 409 y ni el token ni la tarjeta quedan en la SQLite. |
| Alcance del token | `test_allowance_rejects_other_checkout_merchant_and_currency`: se rechazan otro checkout, otro comercio u otra moneda. |
| Vencimiento y token desconocido | `test_expired_allowance_and_unknown_token_fail_closed`: una autorización vencida no recibe token y un token desconocido no aprueba un pago. |
| Rechazo de la tarjeta y conflicto | `test_decline_and_conflicting_delegation`: la tarjeta de rechazo termina en `declined` y reusar la clave de idempotencia con otro contenido recibe 409. |
| AP2 con el comprador presente | `tests/test_ap2_payment.py`: se rechazan una firma ajena, un mandato ausente, un monto o vencimiento alterados, un token emitido antes de exigir AP2 y llaves privadas configuradas como llaves de confianza. |
| AP2 en compras delegadas | La tabla `ALTERATIONS` de `tests/test_ap2_hnp_payment.py`, donde cada fila altera una parte de la cadena: firmas, `aud`, `nonce`, `sd_hash`, el checkout, el monto, el comercio, el instrumento, restricciones desconocidas y el vencimiento. Ningún rechazo emite un token. |
| Reintentos con AP2 | Reintentar un rechazo devuelve un recibo con el mismo identificador de pago; repetir una operación aceptada con la misma clave sigue valiendo después de vencer el mandato; una clave nueva se rechaza. |

Lo que estas pruebas no cubren:

- La compra completa entre agente, comercio y proveedor por red. Eso lo recorre la suite de
  sistema del agente.
- Concurrencia o caídas entre procesos.
- Un Bearer ausente o malformado en cada ruta, ni todas las fronteras de monto y vencimiento en
  cada endpoint.
