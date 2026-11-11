"""exp/（实验脚本）不能把数据目录指到某个写死的真实曲库：测试进程和别的检出（worktree）会误用真实数据。"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_exp_does_not_hardcode_a_data_dir():
    for f in sorted((ROOT / "exp").glob("*.py")):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"tab-trainer[^\"']*/data", text), f"{f.name} 里写死了曲库路径"
        assert "TAB_TRAINER_DATA" not in text or f.name == "__init__.py", f"{f.name} 自己设了数据目录"


def test_importing_exp_leaves_the_data_dir_alone():
    env = {k: v for k, v in os.environ.items() if k != "TAB_TRAINER_DATA"}
    code = "import os, exp, exp.dataset; from app import store; print(os.environ.get('TAB_TRAINER_DATA')); print(store.DATA)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    env_value, data = out.stdout.split("\n")[:2]
    assert env_value == "None"
    assert Path(data) == ROOT / "data"  # 和 app 一样：这个检出目录下的 data/
