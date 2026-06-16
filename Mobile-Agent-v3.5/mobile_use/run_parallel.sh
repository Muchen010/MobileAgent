#!/bin/bash
# =============================================================================
# Run Mobile-Agent on multiple devices concurrently.
# Each device gets its own process, its own screenshot dir (already isolated by
# the script via device serial), and its own log file under /tmp.
# =============================================================================

set -u
cd "$(dirname "$0")"

# --- Devices to drive in parallel (edit this list) ---------------------------
DEVICES=("3AP0224B08101666" "94GVB22B24006428")

# --- Shared task ------------------------------------------------------------
INSTR="在抖音 APP 中发布一条作品。步骤：1)从桌面打开抖音 APP；2)点击底部中间的加号按钮进入发布页面；3)切换到底部的相册tab；4)选择相册里的第一个视频或图片素材；5)点击右上角或右下角的下一步按钮，如果出现编辑界面再点一次下一步进入最终发布编辑页；6)在文案输入框中输入文字 test；7)关闭键盘后必须真实点击底部红色的发布按钮把作品发出去；8)等待出现发布成功的提示或自动跳转到作品列表后，再用 terminate 动作并 status=success 结束。"
ADDINFO="账号已经登录无需登录。不要添加话题、地点、艾特好友、音乐、贴纸。可见范围保持默认不要修改。最关键的一步：必须真实执行 click 动作点击底部的发布按钮把作品发出去，不能在没有点击发布按钮的情况下直接 terminate。如果遇到弹窗如权限请求或活动推荐请选择允许或跳过。如果输入文案后键盘挡住了发布按钮请先点击文案框外的空白处或按返回键收起键盘。"
MAX_STEPS=22

echo "=== Launching ${#DEVICES[@]} agents in parallel ==="
START=$(date +%s)
PIDS=()
for D in "${DEVICES[@]}"; do
    LOG="/tmp/ma_${D}.log"
    python3 -u run_gui_owl_1_5_for_mobile.py \
        --device "$D" \
        --max_steps "$MAX_STEPS" \
        --instruction "$INSTR" \
        --add_info "$ADDINFO" > "$LOG" 2>&1 &
    PID=$!
    PIDS+=("$PID")
    echo "  device $D -> PID $PID (log: $LOG)"
done

echo "Waiting for all agents to finish..."
for PID in "${PIDS[@]}"; do
    wait "$PID"
done
END=$(date +%s)
echo ""
echo "=== All agents finished in $((END-START))s ==="
