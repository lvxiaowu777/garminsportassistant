"""启动器（唯一还留在 ``src/`` 的文件）。

只做一件事：把项目根放进 ``sys.path``，然后调用 ``garmin_coach.server.main()``。
保留这个文件是因为部署脚本（``deploy.py``）和文档里写的都是 ``python src/server.py``。

也可以用包的方式启动：在项目根执行 ``python -m garmin_coach.server``。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from garmin_coach.server import app, main  # noqa: E402

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
