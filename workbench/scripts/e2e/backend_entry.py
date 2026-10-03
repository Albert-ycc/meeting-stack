"""浏览器回归用的后端入口：包一层声档自己的命令行，先关掉 .env 的读取。

.env 是按仓库位置找的（见 config.py 的 ENV_FILES），在生产 checkout 里跑会读到生产的配置。环境变量只能盖掉
run.py 写出来的那几项，没写到的项会从 .env 漏进来（比如数据库路径、日志文件），所以这里直接不读。
用法和 `python -m meeting_workbench.cli` 一样：backend_entry.py serve | scan。
"""

import sys

from meeting_workbench.config import Settings

Settings.model_config["env_file"] = None

from meeting_workbench.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
