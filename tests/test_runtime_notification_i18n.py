import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from notifications.telegram import build_translator


@pytest.mark.parametrize("locale", ["zh-CN", "zh_TW", "ZH-hans", " zh "])
def test_chinese_locale_variants_use_chinese(locale):
    assert build_translator(locale)("strategy_label", name="test") == build_translator("zh")("strategy_label", name="test")


def runtime_function(name, **overrides):
    tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = dict(os=os, request=SimpleNamespace(method="GET", path="/main"),
                     NOTIFY_LANG="zh-CN", SERVICE_NAME="Test service", SECRET_NAME="Test secret alias",
                     STRATEGY_PROFILE="test_strategy", ACCOUNT_REGION="SG", ACCOUNT_GROUP="Test account",
                     strategy_display_name="Test strategy", build_translator=build_translator,
                     t=build_translator("zh-CN"))
    namespace.update(overrides)
    exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_startup_alert_is_compact_localized_and_does_not_embed_provider_text(locale, monkeypatch):
    monkeypatch.setenv("NOTIFY_LANG", locale)
    monkeypatch.delenv("QSL_NOTIFY_LANG", raising=False)
    monkeypatch.setenv("STRATEGY_PROFILE", "test_strategy")
    fn = runtime_function("_runtime_error_notification_message", NOTIFY_LANG=locale, t=build_translator(locale))
    message = fn(RuntimeError("PRIVATE_PROVIDER_SENTINEL"))
    assert len(message.splitlines()) <= 5
    assert "PRIVATE_PROVIDER_SENTINEL" not in message
    assert "未提交订单" not in message
    assert "no order" not in message.lower()
    assert ("未正常结束" in message) is locale.startswith("zh")
    assert ("did not finish successfully" in message) is (locale == "en")
    assert "test_strategy" in message or "Test strategy" in message
