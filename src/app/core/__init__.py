"""两个 Agent 共享的横切能力（不 import 任何业务包）。

归属约定：
- 只放「SKU Agent 与 Image Agent 都要用」而且与业务无关的东西；
- 这里不允许 import `app.agents*` / `app.tools*` / `app.memory*` / `app.agents_image*`，
  由 `tests/test_image_decoupling.py` 静态扫描强制。
"""
