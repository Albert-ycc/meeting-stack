"""pytest 配置里的告警过滤：只吞 starlette 那一条「用 httpx 配 TestClient」的弃用告警，别的弃用告警照常报。

过滤写在 pyproject.toml 的 [tool.pytest.ini_options]。这里取真实的配置去跑：
- 子进程里真的导入 starlette.testclient，看那条告警有没有被吞（走的是库自己发告警的那条路，库改了措辞这里会红）；
- 另起一个小项目，显式发几种告警，看被吞的只有那一种。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

WORKBENCH = Path(__file__).resolve().parents[2]
HTTPX_WARNING = "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead."


def project_filters() -> list[str]:
    config = tomllib.loads((WORKBENCH / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["pytest"]["ini_options"].get("filterwarnings", [])


def make_project(pytester, filters: list[str], tests: str) -> None:
    pytester.makepyprojecttoml(
        "[tool.pytest.ini_options]\nfilterwarnings = " + json.dumps(filters, ensure_ascii=False)
    )
    pytester.makepyfile(tests)


def warning_count(result) -> int:
    return result.parseoutcomes().get("warnings", 0)


def test_importing_testclient_with_only_httpx_installed_raises_no_warning(pytester):
    # 只装了 httpx 没装 httpx2：starlette.testclient 导入时发一条 StarletteDeprecationWarning。
    # 按规矩不装新依赖，所以在 pytest 配置里把它过滤掉；以后装了 httpx2，这条告警不会再出现，
    # 先红的是下面的对照，到时把 pyproject.toml 里的这条过滤和本用例一起删掉。
    tests = """
        def test_import():
            from starlette.testclient import TestClient  # noqa: F401
    """
    make_project(pytester, [], tests)
    control = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    control.assert_outcomes(passed=1)
    assert warning_count(control) == 1, (
        "没配过滤时导入 starlette.testclient 应该有一条告警；没有了说明环境里装了 httpx2，"
        "把 pyproject.toml 里的这条过滤和本用例一起删掉"
    )
    control.stdout.fnmatch_lines([f"*StarletteDeprecationWarning: {HTTPX_WARNING}*"])

    pytester.path.joinpath("pyproject.toml").unlink()
    make_project(pytester, project_filters(), tests)
    filtered = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    filtered.assert_outcomes(passed=1)
    assert warning_count(filtered) == 0


def test_the_filter_swallows_only_that_one_warning(pytester):
    tests = f"""
        import warnings

        from starlette.exceptions import StarletteDeprecationWarning


        def test_httpx_with_testclient():
            warnings.warn({HTTPX_WARNING!r}, StarletteDeprecationWarning)


        def test_other_starlette_deprecation():
            warnings.warn(
                "You should not use the 'timeout' argument with the TestClient.",
                StarletteDeprecationWarning,
            )


        def test_same_text_but_another_category():
            warnings.warn({HTTPX_WARNING!r}, UserWarning)


        def test_another_deprecation_entirely():
            warnings.warn("old thing", DeprecationWarning)
    """
    make_project(pytester, project_filters(), tests)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=4)
    # 四种里只有第一种被吞：别的 starlette 弃用告警、同样措辞但类别不同的、别的弃用告警都还在
    assert warning_count(result) == 3
    output = result.stdout.str()
    assert f"StarletteDeprecationWarning: {HTTPX_WARNING}" not in output
    assert "StarletteDeprecationWarning: You should not use the 'timeout' argument" in output
    assert f"UserWarning: {HTTPX_WARNING}" in output
    assert "DeprecationWarning: old thing" in output
