# Payment provider sandbox

This repository is an independent synthetic PSP for the ACP thesis. Python 3.12 and `uv`.
Run `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, and
`uv run pytest` before handing off changes.

No real card credentials, CVC, charges, settlement, or refunds. The provider owns delegation,
token scope, expiry, single-use confirmation, and test outcomes. Tokens must remain opaque to
the agent and merchant; neither service may infer payment success from token text. Do not store
raw cards, bearer credentials, or tokens in SQLite, logs, or committed files. Preserve separate
agent and merchant authentication and idempotent effectful POSTs.
