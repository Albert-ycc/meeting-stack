"""relay 读 .env 的位置要和工作台 config.py 一致。

两边各写了一遍（relay 是独立部署的，不能 import 工作台）：对不上就是 relay 和工作台读了不同的文件，
表现为「.env 里写了，工作台认、relay 不认」。
"""

import importlib.util

from meeting_workbench import config

RELAY_ENV = config.REPO_ROOT / "relay" / "quickstart" / "relay_env.py"


def _load_relay_env():
    spec = importlib.util.spec_from_file_location("relay_env_contract", RELAY_ENV)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_relay_reads_the_same_env_files_in_the_same_order_as_the_workbench():
    relay_env = _load_relay_env()

    assert relay_env.REPO_ROOT == config.REPO_ROOT
    assert relay_env.ENV_FILES == config.ENV_FILES
