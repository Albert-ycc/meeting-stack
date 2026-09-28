"""4a：测试护栏本身。测试环境里第四期关着、没有 AI key、不读 .env、飞书清空，发给真 AI 的请求
一定让测试失败。"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.tasks import llm_ready


def test_phase_four_is_off_and_no_ai_is_configured():
    settings = Settings()
    assert settings.links_enabled is False
    assert llm_ready(settings) is False
    assert not settings.llm_api_key_file.exists()
    assert Settings.model_config["env_file"] is None
    assert settings.llm_api_base.startswith("http://127.0.0.1")


def test_dotenv_in_the_working_directory_is_ignored(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "MEETING_WORKBENCH_LARK_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/x\n"
        "MEETING_WORKBENCH_PORT=9999\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.lark_webhook_url == ""
    assert settings.port == 8765
    assert (settings.lark_chat_id, settings.lark_app_id) == ("", "")
    assert not Path(settings.lark_cli_bin).exists()
    assert settings.lark_app_secret_file is not None and not settings.lark_app_secret_file.exists()


def test_guard_blocks_real_ai_and_lets_other_urls_through(tmp_path, _no_real_llm):
    for url in (
        "https://api.deepseek.com/chat/completions",
        "https://api.anthropic.com/v1/messages",
        "http://127.0.0.1:9/v1/chat/completions",
    ):
        with pytest.raises(AssertionError, match="测试里不许调真的 AI"):
            urllib.request.urlopen(urllib.request.Request(url, data=b"{}"), timeout=1)
    assert _no_real_llm == [
        "https://api.deepseek.com/chat/completions",
        "https://api.anthropic.com/v1/messages",
        "http://127.0.0.1:9/v1/chat/completions",
    ]
    _no_real_llm.clear()  # 这一例是故意的，不让 teardown 失败
    page = tmp_path / "page.txt"
    page.write_text("ok", encoding="utf-8")
    with urllib.request.urlopen(page.as_uri()) as response:
        assert response.read() == b"ok"


@pytest.mark.allow_local_llm
def test_allow_local_llm_only_lets_local_hosts_through(_no_real_llm):
    with pytest.raises(AssertionError):
        urllib.request.urlopen("https://api.deepseek.com/chat/completions", timeout=1)
    assert _no_real_llm == ["https://api.deepseek.com/chat/completions"]
    _no_real_llm.clear()
    # 本机地址放行（端口 9 上没有服务，连接被拒绝是 URLError，不是护栏）
    with pytest.raises(OSError):
        urllib.request.urlopen("http://127.0.0.1:9/v1/chat/completions", timeout=1)
    assert _no_real_llm == []


def test_a_swallowed_real_ai_call_still_fails_the_test(pytester):
    """调用方把异常吞掉（项目归属、任务抽取都 except Exception）也逃不过：teardown 时失败。"""
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(
        test_leak="""
        from meeting_workbench.config import Settings
        from meeting_workbench.tasks import call_llm


        def test_leak(tmp_path):
            key = tmp_path / "api-key"
            key.write_text("sk-test", encoding="utf-8")
            settings = Settings(
                llm_api_key_file=key,
                llm_api_base="https://api.deepseek.com",
                semantic_enabled=False,
            )
            try:
                call_llm(settings, "你好", system="只回 JSON")
            except Exception:
                pass
        """
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*测试里不许调真的 AI：https://api.deepseek.com/chat/completions*"]
    )
