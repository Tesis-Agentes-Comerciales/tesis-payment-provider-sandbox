# CI-PSP-01: validación independiente

El proveedor usa Python 3.12.14 (`.python-version`) y uv 0.12.15, versiones compatibles con
Python 3.12 del proyecto y con el entorno P2 registrado. No necesita Node, Docker, Merchant,
Agent, servidor externo, claves personales ni `.env` para estas verificaciones.

## Desde un checkout limpio

Clone el repositorio de la organización, cambie al SHA completo que quiera verificar y use
un entorno propio de ese checkout. Desde la raíz, con uv 0.12.15 instalado:

```sh
export UV_BUILD_CONSTRAINT=.github/build-constraints.txt
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS= PYTHONDONTWRITEBYTECODE=1
uv python install
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv build --require-hashes
uv run python -m pytest -p scripts.ci_pytest --junitxml=.local/ci/tests/junit.xml --ci-outcomes=.local/ci/tests/outcomes.json --tb=no --show-capture=no
```

En PowerShell use `$env:UV_BUILD_CONSTRAINT = '.github/build-constraints.txt'`,
`$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'`, `$env:PYTEST_ADDOPTS = ''` y
`$env:PYTHONDONTWRITEBYTECODE = '1'` antes de los mismos comandos uv.
La red solo se necesita para descargar herramientas/dependencias; las pruebas no consultan
proveedores externos. AP2 está fijado por commit en el proyecto/lock. El lock no cambia en
este incremento. Las constraints fijan el backend Hatchling ya requerido y sus dependencias
de build; `uv build --require-hashes` verifica sus wheels. No se agregan dependencias de pagos.
Esto no promete distribuciones idénticas byte a byte entre plataformas.

## Ejecución GitHub

`.github/workflows/ci.yml` corre en cada PR, push a main y despacho manual, en Ubuntu 24.04.
Reutiliza los SHAs de checkout/setup-uv/upload-artifact y uv de Agent/Merchant. Solo necesita
`contents: read`; checkout no persiste credenciales y no se referencian secrets.

- `quality`: formato, lint, tipos (incluidos scripts) y build. Los cuatro checks se intentan
  independientemente después de instalar dependencias, aunque otro check falle.
- `tests`: toda la suite de `tests/`, sin filtros, más regresiones de las guardas de CI.
  No hay exclusiones, `continue-on-error`, relajaciones ni nuevos opt-ins.
- Ambos jobs terminan con un verificador de resultados obligatorios y upload con `always()`.
  Un paso faltante, skipped, cancelado o fallido no equivale a success; ningún resultado local
  sustituye la ejecución de estos jobs en la organización.

El plugin registra nodeid, estado y fases setup/call/teardown. Rechaza colección vacía,
deselección, colección fallida, pruebas no ejecutadas y todo resultado distinto de passed.
Las regresiones levantan subprocesses de pytest con casos deliberados; los fallos internos
esperados prueban la guarda, no son errores ocultados de la suite de pagos.

| Estado | Significado y efecto |
| --- | --- |
| passed | Setup, cuerpo y teardown ejecutados y satisfactorios. |
| failed | Aserción/fallo durante el cuerpo; falla el job. |
| error | Fallo de setup/teardown; colección se informa además por separado; falla el job. |
| skipped | Prueba omitida; es requerida y falla la puerta aunque pytest saliera 0. |
| xfail | Fallo esperado declarado; se conserva y no satisface esta puerta. |
| xpass | Éxito inesperado, estricto o no; se conserva y falla esta puerta. |
| not_executed | Prueba recogida sin sus tres fases completas; no satisface la puerta. |

## Evidencia

Los artefactos `psp-quality-evidence` y `psp-test-evidence` se retienen 14 días. Incluyen
`report.json` y `SHA256SUMS`; tests incluye también JUnit individual y `outcomes.json`.
El reporte conserva SHA efectivamente probado, SHA del head del PR y SHA de Actions por
separado, run ID/intento, versiones y hashes de fuentes/tests/scripts/locks/constraints y
de distribuciones cuando existen. Un PR usa normalmente un merge temporal: no llamarlo head.
Los dos jobs publican resumen. El reporte final comprueba correspondencia de JUnit y fases.

El upload solo permite esos directorios: no incluye SQLite, `.env`, claves ni instrumentos.
JUnit conserva identidades, estados y tiempos; se eliminan capturas y valores de excepciones/
aserciones que podrían contener tokens sintéticos. No se suprimen fallos ni se alteran tests.
`--tb=no --show-capture=no` evita publicar cuerpos de fallos/capturas en el terminal.
Para diagnosticar, use nodeid/fase y reproduzca con datos sintéticos en un checkout aislado.
Cancelación forzada o fallo del runner pueden impedir el upload: ausencia de evidencia nunca
es validación. Antes de expirar el artefacto, conservar el paquete sanitizado y sus hashes
en la organización mediante revisión humana; CI no hace commits ni publicaciones duraderas.
Verificación desde la carpeta descargada: `sha256sum -c SHA256SUMS`.

## Controles de pagos existentes y límites

| Propiedad | Evidencia de prueba existente |
| --- | --- |
| Identidad Agent/Merchant | `test_wrong_actor_cannot_issue_or_confirm`: bearer del rol opuesto devuelve 401. |
| Token opaco, uso único, monto e idempotencia | `test_token_is_opaque_scoped_single_use_and_idempotent`: exceso de monto 422, replay idéntico, otra key 409, token/PAN sintético ausentes de SQLite. |
| Scope | `test_allowance_rejects_other_checkout_merchant_and_currency`: checkout, merchant o moneda ajenos rechazados. |
| Expiración y token desconocido | `test_expired_allowance_and_unknown_token_fail_closed`. |
| Rechazo comercial y conflicto | `test_decline_and_conflicting_delegation`: tarjeta sintética rechazada y contenido conflictivo. |
| AP2 HP | Firma ajena, mandato ausente, monto/TTL alterados, token legacy bajo enforcement y trust keys privadas rechazados. |
| AP2 HNP | Tabla `ALTERATIONS` y tests asociados: firmas, aud/nonce/sd_hash, checkout, montos/payee/instrument, restricciones desconocidas y vencimiento; cero delegaciones al rechazo. |
| Replay AP2 | Retry de rechazo conserva identidad; replay de operación aceptada con la misma key sigue válido tras expirar el mandato; key nueva se rechaza. |

La suite prueba el PSP real en proceso con SQLite temporal y firmas generadas para pruebas.
No demuestra Agent–Merchant–PSP por red ni semántica de UX03. No hay pruebas dedicadas de
concurrencia/crash entre procesos; la matriz HTTP de autenticación no cubre sistemáticamente
bearer ausente/malformado en cada ruta; no hay matriz exhaustiva de fronteras de monto/expiry
para cada endpoint. Se registran como brechas, sin ampliar funciones ni debilitar aserciones.
La generación aleatoria de claves y un test HP que usa UTC actual impiden afirmar replay
bit a bit del material criptográfico; no se cambian esas pruebas en esta unidad.
