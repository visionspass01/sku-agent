"""轻量电商数据中台（Demo）。

分层：
- 数据接入：mock_sources 生成 ERP/CRM/订单模拟快照，ingest 入湖并脱敏；
- 存储计算：SQLite + airflow_mock（模拟调度）+ dbt_like（模拟 dbt 转换）；
- 数据治理：governance 注册指标口径（动销率/安全库存/预计售罄）与脱敏规则；
- 数据服务：FastAPI（:8001）RESTful 接口 + Redis TTL 抖动缓存。
"""

__version__ = "0.1.0"
