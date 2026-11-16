"""守卫测试：把“大家都记得”的约定变成会失败的检查（来由见 LESSONS.md）。

- scripts/ 和 exp/ 删曲子目录只能走 scripts/evalsongs.py；
- evalsongs 只让脚本动 eval- 开头的评测曲，以及这次运行里自己刚建的曲子。
"""

import re
import sys
from pathlib import Path

import pytest

from scripts import evalsongs, make_real_eval

ROOT = Path(__file__).resolve().parents[1]
_DELETES = re.compile(r"\brmtree\s*\(|\brm\s+-[a-zA-Z]*r")


def test_scripts_delete_folders_only_through_evalsongs():
    bad = []
    for top in ("scripts", "exp"):
        for f in sorted((ROOT / top).rglob("*")):
            if f.suffix not in (".py", ".sh", ".mjs", ".command") or f == ROOT / "scripts" / "evalsongs.py":
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if _DELETES.search(line):
                    bad.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not bad, "直接删目录，改用 scripts/evalsongs.py 的 clear_target / discard_created：\n" + "\n".join(
        bad
    )


def _song(data_dir, sid):
    d = data_dir / "songs" / sid
    d.mkdir(parents=True)
    (d / "song.json").write_text("{}")
    return d


@pytest.mark.parametrize(
    "dst",
    ["example-wuting", "20261109-120000-abcdef", "eval-", "eval-../example-wuting", "Eval-x", "", "eval-a/b"],
)
def test_a_target_outside_the_eval_namespace_is_refused_and_kept(data_dir, dst):
    kept = _song(data_dir, "example-wuting")
    with pytest.raises(SystemExit):
        evalsongs.clear_target(dst)
    assert (kept / "song.json").exists()


def test_a_target_equal_to_its_source_is_refused_and_kept(data_dir):
    src = _song(data_dir, "eval-amy")
    with pytest.raises(SystemExit):
        evalsongs.clear_target("eval-amy", src_id="eval-amy")
    assert (src / "song.json").exists()


def test_a_narrower_prefix_refuses_other_eval_songs(data_dir):
    other = _song(data_dir, "eval-amy")
    with pytest.raises(SystemExit):
        evalsongs.clear_target("eval-amy", src_id="eval-romance", prefix="eval-d-")
    with pytest.raises(SystemExit):
        evalsongs.check_target("songs-x", prefix="songs-")  # 前缀本身也得在 eval- 里
    assert (other / "song.json").exists()


def test_an_eval_target_is_cleared_and_nothing_else(data_dir):
    old, user = _song(data_dir, "eval-d-amy"), _song(data_dir, "example-wuting")
    dst = evalsongs.clear_target("eval-d-amy", src_id="eval-amy", prefix="eval-d-")
    assert dst == old and not old.exists() and (user / "song.json").exists()


def test_check_target_deletes_nothing(data_dir):
    old = _song(data_dir, "eval-amy")
    assert evalsongs.check_target("eval-amy") == old
    assert (old / "song.json").exists()


def test_only_a_song_made_in_this_run_is_discarded(data_dir, monkeypatch):
    mine = _song(data_dir, "20261116-120000-aaaaaa")
    evalsongs.discard_created("20261116-120000-aaaaaa")
    assert not mine.exists()

    theirs = _song(data_dir, "20261109-120000-bbbbbb")
    monkeypatch.setattr(evalsongs, "_STARTED", theirs.stat().st_mtime + 60)  # 目录比这次运行早
    with pytest.raises(SystemExit):
        evalsongs.discard_created("20261109-120000-bbbbbb")
    with pytest.raises(SystemExit):
        evalsongs.discard_created("../songs")
    assert (theirs / "song.json").exists()


def test_make_real_eval_refuses_a_user_song_as_its_target(data_dir, monkeypatch):
    """11-08 那次事故的形状：目标 id 是用户自己的曲子。脚本要在读、删任何东西之前退出。"""
    user = _song(data_dir, "example-wuting")
    for target in ("example-wuting", "20261109-120000-abcdef"):
        monkeypatch.setattr(sys, "argv", ["make_real_eval.py", "example-wuting", target, "--all"])
        with pytest.raises(SystemExit, match="eval-"):
            make_real_eval.main()
    assert (user / "song.json").read_text() == "{}"
