# 本地处理与达芬奇交接

## 随附处理助手

在技能目录运行以下命令；实际执行时解析为绝对路径，并保持参数引用：

```bash
python3 scripts/prepare_media.py inspect "/项目/raw/S001_v01.mp4"
python3 scripts/prepare_media.py convert "/项目/raw/S001_v01.mp4" "/项目/processed/S001_v01.mov" --orientation landscape --fps 30 --frame-policy cadence --fit contain --profile 422
```

`portrait` 输出 2160×3840；`landscape` 输出 3840×2160。`contain` 保留全画面并补边，`cover` 裁切填满画面；有参考构图时选择符合参考的策略。助手拒绝覆盖已有输出，成功后写入同名的处理记录 JSON。

`cadence` 使用 fps 滤镜复制或丢弃帧以维持原片时长；它不补出真实运动。`interpolate` 使用运动补偿插帧，需检查遮挡、快速动作、脸部和镜头切换处的变形。原片是 30fps 时无需运动插帧。不要用改变播放速度的方法凑 30fps，导致画面与旁白时长变化。

助手使用 Lanczos 缩放，并输出 MOV / ProRes 422；`--profile hq` 改为 ProRes 422 HQ。保留音轨时转为 PCM 48kHz。10bit 中间编码不会凭空增加 8bit 源片的原始色彩信息。处理是一次转码，避免中间反复压制 H.264。

已标记为全范围 `pc` 的输入会进行明确的像素量程映射，输出 ProRes 的视频范围 `tv`，处理记录中注明这项转换。已有色域、传递函数和矩阵标签保持；缺失标签不补猜测值。

HDR（PQ/HLG）需要明确的色彩管理方案；助手会拒绝静默转换，先在 Resolve 设置输入色彩空间并选择保留 HDR 或转换 SDR。未知色彩标签不能当成已证实的 Rec.709。

## 画质改善的选择

FFmpeg 缩放只能重采样。真正的 AI 超分需要可用模型或软件，Codex 可负责调度和批处理；采用前核实当前环境和该工具的输出方式。Resolve Studio 的 Super Scale、AI 降噪与 Speed Warp 是另一条处理路径。存在可见形变、主体错误或严重闪烁时，优先重生成该镜头。

比较同一片段在相同观看大小下的源片与处理片，检查纹理、脸部、运动和时间稳定性。先验收一个代表性样本，再执行本次批次。单纯满足 3840×2160、30fps 只证明输出规格。

## 导入与完成边界

```bash
python3 scripts/project.py handoff "/项目"
```

每次生成新的 `resolve/handoff-NNN/` 目录，含 `edit-plan.csv`、`narration.txt`、`handoff.json` 与 `import-notes.txt`。剪辑表按 manifest 镜头顺序累计时间，可用于人工搭建粗剪；没有声称它是 Resolve 项目文件。已下载原片直接从本地使用，第三步之后不需要再次下载。

在 Resolve 导入素材前设置时间线为本项目尺寸和精确 30fps。横屏可采用 UHD 3840×2160；竖屏设置自定义 2160×3840，并核实安装版本与许可可用性。旁白生成后用实际语音时长调节剪辑表，不将估计时间当最终时间线。

若使用本机 Resolve 脚本接口，先检测当前版本的接口文档、软件启动与接口可连接性；不可连接时仍交付已完成的素材和导入表，明确工程尚未导入。不为假装自动化而修改全局软件设置。最终剪辑、调色、混音和导出需检查 Resolve 的实际结果。

## 技术依据

以下为 2026-10-11 核实的官方入口。涉及具体版本、模型、许可或接口时按需刷新，不在每次策划时全量重查。

- [FFmpeg 缩放](https://ffmpeg.org/ffmpeg-scaler.html)、[帧率与运动插帧](https://ffmpeg.org/ffmpeg-filters.html#fps)、[ProRes 编码](https://ffmpeg.org/ffmpeg-codecs.html#ProRes)。
- [Resolve 免费版与 Studio](https://www.blackmagicdesign.com/products/davinciresolve)、[Studio AI 功能](https://www.blackmagicdesign.com/products/davinciresolve/studio)、[当前支持编码表](https://documents.blackmagicdesign.com/SupportNotes/DaVinci_Resolve_21_Supported_Codec_List.pdf)。
- [即梦网页](https://jimeng.jianying.com/)、[火山引擎即梦 API 快速入门](https://docs.volcengine.com/docs/JimengAI/QuickStart?lang=zh)。API 的开通与凭证是单独的接入步骤。
