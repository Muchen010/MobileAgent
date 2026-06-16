"""
混剪封装 (FFmpeg + edge-tts + Pillow)，无需 AI 大模型，也不依赖 ffmpeg 的 libass。

输入：
    - 若干素材视频片段 (--clips a.mp4 b.mp4 ... 或 --clips_dir 目录)
    - 一段文案 (--text "..." 或 --text_file xxx.txt)

自动完成：
    1. 文案 -> edge-tts 生成配音 mp3
    2. 按句 + 配音时长比例切分；用 Pillow 把每句渲染成透明 PNG 字幕(白字黑描边)
    3. 素材标准化(统一分辨率/帧率)后拼接
    4. 视频循环铺满配音时长 + overlay 字幕(按时间显示) + 混入配音 -> 成品 mp4

依赖：ffmpeg、ffprobe (系统), edge-tts、Pillow (pip)

示例：
    python3 editor_ffmpeg.py \
        --clips a.mp4 b.mp4 \
        --text "世界杯太精彩了，约上好友一起看球，快搜：15世界杯看球" \
        --voice zh-CN-XiaoxiaoNeural --shuffle \
        --output ~/Desktop/mix_output.mp4
"""

import argparse
import asyncio
import os
import random
import subprocess
import sys
import tempfile
import shutil

try:
    import edge_tts
except ImportError:
    sys.exit("[ERROR] 需要 edge-tts: pip3 install edge-tts")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("[ERROR] 需要 Pillow: pip3 install pillow")


FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _find_font():
    for f in FONT_CANDIDATES:
        if os.path.isfile(f):
            return f
    sys.exit("[ERROR] 未找到中文字体，请在 FONT_CANDIDATES 中补充字体路径")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _ffprobe_duration(path):
    rc, out, err = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ])
    if rc != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {err}")
    return float(out.strip())


def _collect_clips(args):
    clips = []
    if args.clips:
        clips = list(args.clips)
    elif args.clips_dir:
        exts = (".mp4", ".mov", ".m4v", ".mkv", ".avi")
        for name in sorted(os.listdir(args.clips_dir)):
            if name.lower().endswith(exts):
                clips.append(os.path.join(args.clips_dir, name))
    if not clips:
        sys.exit("[ERROR] 未找到素材，请用 --clips 或 --clips_dir 指定")
    missing = [c for c in clips if not os.path.isfile(c)]
    if missing:
        sys.exit(f"[ERROR] 素材文件不存在: {missing}")
    if args.shuffle:
        random.shuffle(clips)
    return clips


# --------------------------------------------------------------------------
# 1. TTS
# --------------------------------------------------------------------------
async def _synth_audio(text, voice, audio_path, rate, volume):
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    with open(audio_path, "wb") as audio:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])


def synth_audio(text, voice, audio_path, rate="+0%", volume="+0%"):
    asyncio.run(_synth_audio(text, voice, audio_path, rate, volume))


# --------------------------------------------------------------------------
# 2. 字幕: 分句 + 时间分配 + Pillow 渲染 PNG
# --------------------------------------------------------------------------
def _split_sentences(text):
    import re
    parts = re.split(r"(?<=[。！？!?；;，,、\n])", text)
    sentences = [p.strip().strip("。！？!?；;，,、 ") for p in parts]
    return [s for s in sentences if s] or [text.strip()]


def sentence_times(text, total_dur):
    """返回 [(sentence, start, end), ...]，按字符数比例分配时长。"""
    sentences = _split_sentences(text)
    total_chars = sum(len(s) for s in sentences) or 1
    items, t = [], 0.0
    for s in sentences:
        seg = total_dur * len(s) / total_chars
        start, end = t, min(t + seg, total_dur)
        t = end
        items.append((s, start, end))
    return items


def render_subtitle_png(text, w, h, fontsize, font_path, out_png, margin_bottom):
    """渲染一张全屏透明 PNG，底部居中显示一句字幕(白字黑描边，自动折行)。"""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, fontsize)

    # 自动折行（按视频宽度）
    max_w = int(w * 0.86)
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)

    ascent, descent = font.getmetrics()
    line_h = ascent + descent + 12
    total_h = line_h * len(lines)
    y0 = h - margin_bottom - total_h
    stroke = max(2, fontsize // 14)

    for i, line in enumerate(lines):
        cx = w // 2
        cy = y0 + i * line_h + line_h // 2
        draw.text((cx, cy), line, font=font,
                  fill=(255, 255, 255, 255),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
                  anchor="mm")
    img.save(out_png)


# --------------------------------------------------------------------------
# 3. 标准化 + 拼接素材
# --------------------------------------------------------------------------
def normalize_and_concat(clips, out_path, w, h, fps, fit):
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    parts = []
    for i in range(len(clips)):
        if fit == "cover":
            vf = (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                  f"crop={w}:{h},setsar=1,fps={fps}[v{i}]")
        else:
            vf = (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                  f"setsar=1,fps={fps}[v{i}]")
        parts.append(vf)
    concat_inputs = "".join(f"[v{i}]" for i in range(len(clips)))
    filter_complex = ";".join(parts) + ";" + \
        f"{concat_inputs}concat=n={len(clips)}:v=1:a=0[outv]"
    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", filter_complex,
           "-map", "[outv]", "-an",
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-r", str(fps), out_path]
    rc, out, err = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"标准化拼接失败:\n{err[-1500:]}")


# --------------------------------------------------------------------------
# 4. 合成：循环视频 + overlay 字幕 + 配音
# --------------------------------------------------------------------------
def compose(concat_video, voice_mp3, sub_items, duration, output, bgm=None, bgm_volume=0.25):
    """
    sub_items: [(png_path, start, end), ...]
    用 overlay 链按时间叠加每张字幕 PNG，并混入配音。
    """
    abs_concat = os.path.abspath(concat_video)
    abs_voice = os.path.abspath(voice_mp3)
    abs_output = os.path.abspath(output)

    cmd = ["ffmpeg", "-y",
           "-stream_loop", "-1", "-i", abs_concat,   # 0: video
           "-i", abs_voice]                           # 1: voice
    for png, _, _ in sub_items:
        cmd += ["-i", os.path.abspath(png)]           # 2..N+1: subtitle pngs

    bgm_idx = None
    if bgm:
        bgm_idx = 2 + len(sub_items)
        cmd += ["-stream_loop", "-1", "-i", os.path.abspath(bgm)]

    # overlay 链：逗号在 enable 表达式里需用 \, 转义
    chains, prev = [], "[0:v]"
    for k, (png, s, e) in enumerate(sub_items):
        in_idx = k + 2
        out_lbl = f"[v{k}]"
        chains.append(
            f"{prev}[{in_idx}:v]overlay=0:0:enable=between(t\\,{s:.3f}\\,{e:.3f}){out_lbl}"
        )
        prev = out_lbl
    if not chains:
        chains = ["[0:v]null[vout]"]
        prev = "[vout]"

    filter_complex = ";".join(chains)

    if bgm:
        filter_complex += (f";[{bgm_idx}:a]volume={bgm_volume}[bg];"
                           f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]")
        amap = "[a]"
    else:
        amap = "1:a"

    cmd += ["-filter_complex", filter_complex,
            "-map", prev, "-map", amap,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", abs_output]

    rc, out, err = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"合成失败:\n{err[-1500:]}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="混剪封装 (FFmpeg + edge-tts + Pillow)")
    parser.add_argument("--clips", nargs="+", help="素材视频文件列表")
    parser.add_argument("--clips_dir", help="素材目录")
    parser.add_argument("--text", help="文案")
    parser.add_argument("--text_file", help="文案文件")
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    parser.add_argument("--rate", default="+0%")
    parser.add_argument("--volume", default="+0%")
    parser.add_argument("--bgm", default=None, help="可选背景音乐")
    parser.add_argument("--size", default="1080x1920", help="输出分辨率 WxH")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--fit", choices=["cover", "pad"], default="cover")
    parser.add_argument("--fontsize", type=int, default=58, help="字幕字号")
    parser.add_argument("--margin_bottom", type=int, default=160, help="字幕距底边像素")
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    text = args.text
    if not text and args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read().strip()
    if not text:
        sys.exit("[ERROR] 请用 --text 或 --text_file 提供文案")

    clips = _collect_clips(args)
    w, h = (int(x) for x in args.size.lower().split("x"))
    out_path = args.output or os.path.join(
        os.path.expanduser("~/Desktop"), "mix_output.mp4")
    font_path = _find_font()

    workdir = tempfile.mkdtemp(prefix="mix_")
    try:
        print(f"[1/4] 文案转配音 (voice={args.voice}) ...")
        voice_mp3 = os.path.join(workdir, "voice.mp3")
        synth_audio(text, args.voice, voice_mp3, rate=args.rate, volume=args.volume)
        dur = _ffprobe_duration(voice_mp3)
        print(f"      配音时长 = {dur:.2f}s")

        print(f"[2/4] 渲染字幕 PNG (字体: {os.path.basename(font_path)}) ...")
        times = sentence_times(text, dur)
        sub_items = []
        for idx, (sent, s, e) in enumerate(times):
            png = os.path.join(workdir, f"sub_{idx}.png")
            render_subtitle_png(sent, w, h, args.fontsize, font_path, png,
                                args.margin_bottom)
            sub_items.append((png, s, e))
        print(f"      共 {len(sub_items)} 句字幕")

        print(f"[3/4] 标准化拼接 {len(clips)} 个素材 -> {w}x{h}@{args.fps} ({args.fit}) ...")
        concat_video = os.path.join(workdir, "concat.mp4")
        normalize_and_concat(clips, concat_video, w, h, args.fps, args.fit)
        print(f"      素材总时长 = {_ffprobe_duration(concat_video):.2f}s (循环铺满配音)")

        print(f"[4/4] 合成(视频+字幕overlay+配音{'+BGM' if args.bgm else ''}) ...")
        compose(concat_video, voice_mp3, sub_items, dur, out_path, bgm=args.bgm)

        size_mb = os.path.getsize(out_path) / 1024 / 1024
        print(f"\n[DONE] 成品已保存: {out_path}  ({size_mb:.2f} MB, {dur:.1f}s)")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
