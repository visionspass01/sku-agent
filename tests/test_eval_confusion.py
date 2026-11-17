"""二分类混淆矩阵（`app.pipeline.metrics.binary_confusion`）单测。

复审 N7：评测脚本最容易被高估的地方，就是把"跑失败/没预案"的样本拿默认值补一个预测，
塞进矩阵里当 TP/FP。这里把"未判定不进矩阵"这条语义钉死。
"""

from __future__ import annotations

from app.pipeline.metrics import binary_confusion


def _row(truth, pred):
    return {"truth_pos": truth, "pred_pos": pred}


def test_basic_confusion():
    rows = [
        _row(True, True), _row(True, True),     # TP x2
        _row(True, False),                      # FN x1
        _row(False, True),                      # FP x1
        _row(False, False), _row(False, False),  # TN x2
    ]
    stats = binary_confusion(rows)
    assert (stats["tp"], stats["fp"], stats["fn"], stats["tn"]) == (2, 1, 1, 2)
    assert stats["judged"] == 6 and stats["unjudged"] == 0
    assert stats["accuracy"] == round(4 / 6, 3)
    assert stats["precision"] == round(2 / 3, 3)
    assert stats["recall"] == round(2 / 3, 3)


def test_unjudged_rows_are_excluded_from_matrix():
    """pred 为 None（跑失败/没预案）= 未判定：既不进 TP/FP，也不进分母。"""
    rows = [
        _row(True, True),
        _row(True, None),    # 跑失败的真阳性样本：不能算 TP，也不能算 FN
        _row(False, None),   # 跑失败的真阴性样本
    ]
    stats = binary_confusion(rows)
    assert (stats["tp"], stats["fp"], stats["fn"], stats["tn"]) == (1, 0, 0, 0)
    assert stats["judged"] == 1
    assert stats["unjudged"] == 2
    assert stats["precision"] == 1.0 and stats["recall"] == 1.0


def test_empty_rows_are_safe():
    stats = binary_confusion([])
    assert stats["judged"] == 0 and stats["unjudged"] == 0
    assert stats["accuracy"] == 0.0 and stats["precision"] == 0.0
    assert stats["f1"] == 0.0


def test_all_unjudged_gives_zero_metrics():
    stats = binary_confusion([_row(None, None), _row(True, None)])
    assert stats["judged"] == 0
    assert stats["unjudged"] == 2
    assert stats["accuracy"] == 0.0 and stats["recall"] == 0.0
