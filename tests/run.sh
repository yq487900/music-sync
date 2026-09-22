#!/bin/bash
# 在容器内跑自测
#   ./tests/run.sh        核心链路（音质协商 / 命名 / 下载校验 / 标签 / NFO）
#   ./tests/run.sh lx     洛雪音源沙箱（加载 / 取链 / 越权拦截 / 超时）
#   ./tests/run.sh org    整理页（扫描 / 刮削 / ID 匹配 / 保存 / 回收站）
#   ./tests/run.sh ci     云盘索引（歌单页「在不在云盘」的判定）
#   ./tests/run.sh cal    上传后按歌单数据校准（封面/专辑/歌词）
#   ./tests/run.sh up     上传成功后立刻登记云盘索引（不碰真实云盘）
#   ./tests/run.sh mon    歌单监控的判定逻辑（不联网、不写真实库）
#   ./tests/run.sh pl     歌单索引（在不在歌单里 / 新歌判定）
#   ./tests/run.sh all    全部
cd "$(dirname "$0")/.." || exit 1

run_one() {
    local file="$1"
    docker cp "tests/$file" "music-sync:/tmp/$file" >/dev/null || return 1
    docker exec music-sync python "/tmp/$file"
}

case "${1:-core}" in
    lx)  run_one lxselftest.py ;;
    org) bash tests/organize_test.sh ;;
    ci)  run_one cloud_index_test.py ;;
    cal) run_one calibrate_test.py ;;
    up)  run_one upload_test.py ;;
    mon) run_one monitor_test.py ;;
    pl)  run_one playlist_index_test.py ;;
    all) run_one selftest.py && run_one lxselftest.py && run_one cloud_index_test.py \
         && run_one calibrate_test.py && run_one upload_test.py && run_one monitor_test.py \
         && run_one playlist_index_test.py && bash tests/organize_test.sh ;;
    *)   run_one selftest.py ;;
esac
