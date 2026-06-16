# 短视频「AI 生成 → 多设备自动发布」自动化流水线 设计文档

> 目标：搭建一条从**视频产出**到**多设备自动发布**的端到端流水线。
> 视频产出提供 3 种能力（自上传 / AI 生成 / 剪辑混剪），发布时从视频池**随机取片**，并发投递到多台真机。
> 原则：**能用成熟开源就不重复造轮子**，自研只做"粘合层"和业务编排。

---

## 1. 总体架构

```
┌──────────────────────── 生产层 (Producer) ────────────────────────┐
│  A1 视频自上传        A2 AI 文生视频        A3 剪辑/混剪生成        │
│  (用户投素材)         (Seedance 已实现)     (FFmpeg/MoviePy)         │
└───────────────┬───────────────┬───────────────┬───────────────────┘
                │               │               │
                ▼               ▼               ▼
        ┌───────────────────────────────────────────────┐
        │            视频池 VideoPool (统一入库)          │
        │   videos/ + index.json (来源/状态/hash/文案)   │
        └───────────────────────┬───────────────────────┘
                                 │  随机取未发布片
                                 ▼
        ┌───────────────────────────────────────────────┐
        │          文案生成 Caption (LLM: doubao-seed)    │
        │     标题 / 正文 / 话题#，随机模板防同质化       │
        └───────────────────────┬───────────────────────┘
                                 ▼
┌──────────────────────── 发布层 (Publisher) ───────────────────────┐
│  设备池 DeviceManager → adb push 素材 → Agent 自动发布 → 结果校验   │
│  (复用 run_parallel.sh + run_gui_owl_1_5_for_mobile.py，多设备并发) │
└───────────────────────────────────────────────────────────────────┘
                                 ▲
                                 │
        ┌───────────────────────────────────────────────┐
        │     调度编排 Scheduler (APScheduler/Celery)     │
        │   定时触发 · 设备限流 · 失败重试 · 状态机记录    │
        └───────────────────────────────────────────────┘
```

**三大层 + 两个横切**：生产层、视频池、发布层；横切为「文案生成」和「调度编排」。

---

## 2. 模块设计

### 模块 A：视频生产（3 种能力）

| 子能力 | 说明 | 现状 | 实现方式 |
|---|---|---|---|
| **A1 视频自上传** | 用户把已有 mp4 丢进 `videos/inbox/`，或提供上传接口 | 待建 | 简单文件监听 + 入库（自研，<50 行） |
| **A2 AI 文生视频** | 文案/分镜 → Seedance 生成 | ✅ **已实现** | [`video_gen/gen_video_seedance.py`](video_gen/gen_video_seedance.py) |
| **A3 剪辑/混剪生成** | 多段素材拼接、加字幕、配乐、转场、变速 | 待建 | **FFmpeg / MoviePy**（开源，见 §4） |

> A2 已经跑通：火山 Runway 网关 + `doubao-seedance-2-0-260128`，支持时长/比例/音频参数。

#### A3 剪辑能力建议拆为几个原子操作（全部由 FFmpeg 完成）：
- **拼接**：多个片段首尾相接
- **混剪**：从素材库随机抽 N 段 + 随机顺序（防重复指纹）
- **加字幕**：烧录 SRT / ASS（配合 Whisper 自动生成字幕）
- **配乐**：叠加 BGM（音量 ducking）
- **去重处理**：随机裁剪首尾、轻微变速、镜像、调色 → 降低平台「搬运/重复」判定

---

### 模块 B：视频池 VideoPool

统一存储 + 元数据索引，是连接「生产」和「发布」的中枢。

**目录结构：**
```
video_gen/pool/
├── inbox/            # A1 用户上传的原始素材
├── raw_clips/        # A3 混剪用的素材片段库
├── ready/            # 已就绪、待发布的成品视频
├── published/        # 已发布归档
└── index.json        # 元数据索引（状态机）
```

**`index.json` 每条记录（数据模型）：**
```json
{
  "id": "vid_20260616_001",
  "path": "ready/vid_20260616_001.mp4",
  "source": "ai | upload | edit",        // 三种来源
  "sha256": "abc123...",                  // 去重指纹
  "duration": 15,
  "caption": "世界杯看球攻略...",          // 关联文案(可空,发布时再生成)
  "topics": ["#世界杯", "#看球"],
  "status": "ready | publishing | published | failed",
  "published_devices": ["94GVB...", "3AP0..."],
  "created_at": "2026-06-16T11:00:00",
  "published_at": null
}
```

**随机取片逻辑：** 从 `status == ready` 中随机选 1 条 → 标记 `publishing` → 发布成功标 `published` 并记录设备，失败回滚 `ready`。同一视频可配置「每设备只发一次」避免重复。

---

### 模块 C：多设备发布 Publisher

| 子模块 | 现状 | 说明 |
|---|---|---|
| 多设备并发 | ✅ **已实现** | [`run_parallel.sh`](Mobile-Agent-v3.5/mobile_use/run_parallel.sh) + [`run_gui_owl_1_5_for_mobile.py`](Mobile-Agent-v3.5/mobile_use/run_gui_owl_1_5_for_mobile.py) |
| 截图目录隔离 | ✅ 已实现 | task_dir 带 device serial |
| **素材 push 到相册** | 待建 | `adb push <video> /sdcard/DCIM/Camera/` + 媒体扫描广播 |
| **发布成功校验** | 待加强 | 检测当前 Activity 是否跳到 `UltraDetailActivity`/主页才算成功 |
| **设备池管理** | 待建 | 维护设备状态(空闲/忙/异常)、账号绑定、限流计数 |

#### 关键补强点（基于前期实测经验）：
1. **素材分发**：发布前把选中的视频 `adb push` 进每台设备相册，并触发：
   ```bash
   adb -s <device> push video.mp4 /sdcard/DCIM/Camera/
   adb -s <device> shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/DCIM/Camera/video.mp4
   ```
2. **发布成功验证**：实测 Agent 偶尔会"嘴上说点发布、手上直接 terminate"。需在脚本里加：terminate 前检测当前 Activity，未跳转到详情页/主页则强制再点发布。
3. **设备级限流**：每设备每天 ≤ N 条、间隔 ≥ M 分钟（防风控，见 §7）。

---

### 模块 D：文案生成 Caption

复用现有 MAAS 网关（`doubao-seed-2-0-pro` 对话模型）：
- 输入：视频主题/标签 → 输出：标题 + 正文 + 话题#
- **随机模板池**：准备 10+ 文案模板，每次随机组合，避免多账号同质化被关联
- 已有可用通道：`https://runway.devops.xiaohongshu.com/openai/doubao/chat/completions`

---

### 模块 E：调度编排 Scheduler

| 能力 | 推荐方案 |
|---|---|
| 定时触发 | **APScheduler**（轻量，单机够用）/ **Celery**（重，分布式） |
| 任务队列 | Celery + Redis（规模大时） |
| 失败重试 | Celery 自带 retry / 自研重试装饰器 |
| 状态记录 | SQLite（轻量）/ PostgreSQL |

---

## 3. 端到端数据流

```
1. [生产] A1/A2/A3 产出 mp4 → 写入 pool/ready/ + index.json (status=ready)
2. [调度] Scheduler 定时唤醒，检查空闲设备 + 限流额度
3. [取片] VideoPool 随机选 1 条 ready 视频 (status→publishing)
4. [文案] Caption 用 LLM 生成标题/正文/话题
5. [分发] adb push 视频到目标设备相册 + 媒体扫描
6. [发布] Agent 并发执行发布流程 (打开抖音→选相册首个→填文案→发布)
7. [校验] 检测 Activity 跳转，确认发布成功
8. [归档] status→published，移动到 pool/published/，记录设备和时间
   失败则 status→failed/ready，触发重试
```

---

## 4. 开源项目推荐（不要重复造轮子）

### 🎬 视频剪辑 / 混剪（模块 A3 核心）

| 项目 | 用途 | 链接 |
|---|---|---|
| **FFmpeg** | 视频处理瑞士军刀：拼接/转码/裁剪/加字幕/配乐/变速，**首选** | https://github.com/FFmpeg/FFmpeg |
| **ffmpeg-python** | FFmpeg 的 Python 绑定，代码化拼接流水线 | https://github.com/kkroening/ffmpeg-python |
| **MoviePy** | 纯 Python 视频编辑，适合加文字/转场/合成 | https://github.com/Zulko/moviepy |
| **auto-editor** | 自动剪掉静音/无效片段，快速成片 | https://github.com/WyattBlue/auto-editor |

### 📝 自动字幕 / 配音

| 项目 | 用途 | 链接 |
|---|---|---|
| **OpenAI Whisper** | 语音转字幕（ASR），生成 SRT | https://github.com/openai/whisper |
| **faster-whisper** | Whisper 的高速实现（CTranslate2） | https://github.com/SYSTRAN/faster-whisper |
| **edge-tts** | 免费微软 TTS 配音，多音色中文 | https://github.com/rany2/edge-tts |

### 📱 设备控制 / 自动化发布

| 项目 | 用途 | 链接 |
|---|---|---|
| **social-auto-upload** | 多平台自媒体自动发布（抖音/小红书/B站/视频号），**可直接参考其发布逻辑** | https://github.com/dreammis/social-auto-upload |
| **uiautomator2** | Python 操控 Android，比纯 adb 更稳（元素定位/输入） | https://github.com/openatx/uiautomator2 |
| **Appium** | 跨平台 UI 自动化框架（工业级） | https://github.com/appium/appium |
| **scrcpy** | 投屏 + 控制，可做可视化监控面板 | https://github.com/Genymobile/scrcpy |

> 说明：本项目发布层用的是 **GUI-Owl 视觉 Agent**（看截图点按钮），优点是抗 UI 改版、跨 App 通用；
> `social-auto-upload` / `uiautomator2` 是**基于固定坐标/元素**的方案，更快更稳但需为每个 App 适配。
> 二者可**互补**：稳定的固定流程用 uiautomator2，复杂/易变的环节用视觉 Agent 兜底。

### ⚙️ 调度 / 任务队列

| 项目 | 用途 | 链接 |
|---|---|---|
| **APScheduler** | 轻量定时任务（单机首选） | https://github.com/agronholm/apscheduler |
| **Celery** | 分布式任务队列（规模化时） | https://github.com/celery/celery |

### 🤖 进阶：数字人 / 口播视频（可选）

| 项目 | 用途 | 链接 |
|---|---|---|
| **SadTalker** | 单张照片 + 音频 → 口播数字人 | https://github.com/OpenTalker/SadTalker |
| **Wav2Lip** | 音频驱动唇形同步 | https://github.com/Rudrabha/Wav2Lip |

---

## 5. 推荐目录结构（落地后）

```
video_gen/
├── gen_video_seedance.py     # ✅ A2 AI生成 (已实现)
├── .env                       # ✅ Seedance 配置 (已实现)
├── pool/                      # 视频池
│   ├── inbox/  raw_clips/  ready/  published/  index.json
├── producers/
│   ├── upload_watcher.py     # A1 监听上传入库
│   └── editor_ffmpeg.py      # A3 FFmpeg 混剪封装
├── caption/
│   └── gen_caption.py        # D 文案生成 (调 doubao-seed)
├── pool_manager.py            # B 视频池增删查 + 随机取片
└── scheduler.py               # E APScheduler 编排入口

Mobile-Agent-v3.5/mobile_use/
├── run_gui_owl_1_5_for_mobile.py   # ✅ 单设备发布 (已实现)
├── run_parallel.sh                  # ✅ 多设备并发 (已实现)
├── push_to_album.py                 # 待建: 素材分发到相册
└── device_manager.py                # 待建: 设备池 + 限流
```

---

## 6. 实施路线图（分阶段，复用已有成果）

| 阶段 | 内容 | 工作量 | 依赖 |
|---|---|---|---|
| **P0 已完成** | Seedance AI 生成 ✅ · 多设备并发发布 ✅ | — | — |
| **P1 视频池** | `pool_manager.py` + index.json 状态机 + 随机取片 | 小 (1天) | 自研 |
| **P2 素材分发+校验** | `push_to_album.py` + 发布成功 Activity 校验 | 小 (1天) | adb |
| **P3 剪辑能力 A3** | `editor_ffmpeg.py` 混剪/字幕/配乐 | 中 (2-3天) | **FFmpeg** |
| **P4 上传能力 A1** | `upload_watcher.py` 目录监听入库 | 小 (0.5天) | 自研 |
| **P5 文案生成 D** | `gen_caption.py` + 随机模板池 | 小 (1天) | MAAS LLM |
| **P6 调度编排 E** | `scheduler.py` 定时+限流+重试 | 中 (2天) | **APScheduler** |
| **P7 设备池 C** | `device_manager.py` 状态/账号/限流 | 中 (2天) | 自研 |

> 最快可用路径：**P1 + P2** 即可把"已生成的视频 → 随机选 → 多设备发"跑通，约 2 天。

---

## 7. 风控注意事项（重要）

多账号矩阵发布最大的风险不是技术，而是平台风控。务必：

1. **设备指纹隔离**：一机一账号，避免同 WiFi 出口 IP（建议每台独立 4G/代理）
2. **内容去重**：A3 混剪做随机变速/裁剪/调色，避免视频 hash 雷同被判搬运
3. **文案差异化**：D 模块用随机模板池，多账号不发一模一样的文案
4. **发布频率限制**：单号每天 ≤ 3-5 条，间隔 ≥ 30 分钟（在 Scheduler 限流）
5. **账号养护**：新号先养 1-2 周再批量发
6. **拟人化操作**：滑动轨迹用贝塞尔曲线 + 随机间隔（替换当前直线 swipe）
7. **谐音/规避**：敏感引导词用谐音（如"小某书"），已在视频生成中实践

---

## 8. 已落地资产清单

| 资产 | 路径 | 状态 |
|---|---|---|
| AI 视频生成脚本 | [`video_gen/gen_video_seedance.py`](video_gen/gen_video_seedance.py) | ✅ 可用（支持 `--resume` 断点恢复） |
| Seedance 配置 | [`video_gen/.env`](video_gen/.env) | ✅ 已配置网关+模型 |
| 单设备发布 Agent | [`run_gui_owl_1_5_for_mobile.py`](Mobile-Agent-v3.5/mobile_use/run_gui_owl_1_5_for_mobile.py) | ✅ 多设备目录隔离 |
| 多设备并发发布 | [`run_parallel.sh`](Mobile-Agent-v3.5/mobile_use/run_parallel.sh) | ✅ 实测两机并发 104s |
| 配置加载器 | [`env_loader.py`](Mobile-Agent-v3.5/mobile_use/env_loader.py) | ✅ 自动读 .env |

---

*本文档为方案设计。绿色 ✅ 为已实现，其余为待建模块及推荐选型。建议按 §6 路线图从 P1/P2 开始落地。*
