# Mac 使用说明（视频配音 + 合并成片）

本文档汇总 Windows 上已验证的流程，便于在 Mac 上继续工作。  
**Cursor 聊天记录不会跨电脑自动同步**；以本文件 + 文件夹内脚本为准。

---

## 一、Mac 环境准备

### 1. 安装 Homebrew（若尚未安装）

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

按终端提示把 `brew` 加入 PATH（Apple Silicon 常见路径）：

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

### 2. 依赖安装对照表

| 项目 | 必需 | macOS 命令 | 验证 |
|------|------|------------|------|
| Python 3.10+ | 是 | `brew install python@3.12` | `python3 --version` |
| ffmpeg / ffprobe | 是 | `brew install ffmpeg` | `ffmpeg -version` |
| tkinter（弹窗选文件夹） | 建议 | `brew install python-tk@3.12` | 运行 `--pick-folder` 能弹窗 |
| pip 第三方包 | **否** | 无需安装 | — |

**一键安装：**

```bash
brew install python@3.12 ffmpeg python-tk@3.12
```

### 3. 从 Windows 拷到 Mac 的内容

| 用途 | 建议拷贝 |
|------|----------|
| **仅合并视频+配音** | 整个 `merge_dub_tool` 文件夹 |
| **完整 Udemy 配音流水线** | `components` 下脚本 + `tubeget.config.json` + `gemini-code-1780751633188.yaml` + 课程视频目录 |

Mac 上路径示例：`~/Videos/Udemy-AWS/`（路径可与 Windows 不同，不影响脚本）。

---

## 二、合并视频 + 配音（已有 mp4 + mp3）

适用：配音已生成，只需「原视频无声 + 中文配音 → `*_C.mp4`」。

### 工具位置

便携包内文件：

- `merge_dub_videos.py` — 主程序（仅需 Python 标准库 + ffmpeg）
- `merge_dub_videos.sh` — Mac 启动脚本
- `使用说明.txt` — 简要说明

### 使用方法

```bash
cd /path/to/merge_dub_tool
chmod +x merge_dub_videos.sh
./merge_dub_videos.sh
```

| 选项 | 作用 |
|------|------|
| 1 | 图形选文件夹 + **预览**配对（不写文件） |
| 2 | 图形选文件夹 + **正式合并**（默认） |
| 3 | 手动输入目录路径 |

命令行等价：

```bash
python3 merge_dub_videos.py --pick-folder --dry-run   # 预览
python3 merge_dub_videos.py --pick-folder             # 合并
python3 merge_dub_videos.py "/path/to/课程目录"        # 指定目录
```

### 配对规则

同一子文件夹内：

- **视频**：`.mp4` / `.mkv` 等（跳过已生成的 `*_C.mp4`）
- **配音**：`.mp3` / `.m4a` 等
- **文件名前 4 个英文单词相同** → 视为一对

示例：

```
1 - Course Overview.mp4  +  1 - Course Overview.mp3
→ 1 - Course Overview_C.mp4（仅配音轨，原声丢弃）
```

---

## 三、从字幕批量配音（Udemy 课程）

### 1. 目录要求

每个课时文件夹内同时有：

- `xxx.mp4`
- `xxx.en_US.bilingual.srt`

### 2. 推荐配置要点（`tubeget.config.json`）

| 配置项 | 建议值 | 说明 |
|--------|--------|------|
| `kokoro_voice` | `zf_xiaoxiao` | 晓晓普通话，勿用 `zf_xiaobei` |
| `optimize_subtitle` | `false` | 免费通路，不默认耗 Gemini |
| `use_zh_compress_strategy` | `true` | 本地 YAML 压缩长句 |
| `zh_strategy_path` | `gemini-code-1780751633188.yaml` | NotebookLM/Gemini 归纳的策略 |
| `baseline_playback_speed` | `1.35` | 整体提速，口齿仍清晰 |
| `max_playback_speed` | `1.5` | 单句变速上限 |
| `allow_partial_dub` | `true` | ≥98% 仍出片，少数句静音 |
| `gemini_skip_merge` / `gemini_skip_suspicious` | `true` | 减少 API 调用 |

### 3. 运行命令

```bash
cd /path/to/课程根目录   # 含「1 - Introduction」等章节的目录
python3 run_batch_course.py --folders "2" --no-dialog
```

只测第 2 章：把 `"2"` 换成章节名或序号。

### 4. 与 Windows 同步脚本

若在 Windows 的 `C:\video\Youtube\components` 更新了代码，Mac 上需重新拷贝：

- `run_batch_course.py`
- `ai_shorten.py`
- `zh_compress.py`
- `step1_subtitle_to_audio.py`
- `dub_naming.py`
- `tubeget.config.json`
- `gemini-code-1780751633188.yaml`

拷贝后在 Mac 运行：

```bash
python3 check_udemy_sync.py   # 若已拷贝该检查脚本
```

---

## 四、字幕合并（给 LLM 分析用，不影响配音）

```bash
python3 merge_chapter_bilingual.py              # 每章 _merged.en_US.bilingual.srt
python3 merge_chapter_bilingual.py --course-zip-only   # 主目录 zip 全课
```

生成 `_merged.en_US.bilingual.srt`、`_course_merged.en_US.bilingual.zip` **不会修改**原 `.bilingual.srt`，配音不用这些文件。

---

## 五、Cursor 与文档

| 内容 | 是否随账号/Mac 自动同步 |
|------|-------------------------|
| Agent 聊天记录 | **否**（绑定本机工作区路径） |
| 项目内文件（本 md、脚本、配置） | **是**（靠拷贝/Git/网盘） |
| User Rules（账号设置） | 通常 **是** |

在 Mac 新开 Cursor → 打开项目文件夹 → 让 Agent「阅读 `Mac使用说明.md`」即可继续。

---

## 六、常见问题

**Q：弹窗选不了文件夹？**  
A：`brew install python-tk@3.12`，或改用命令行传路径。

**Q：合并时 0 对匹配？**  
A: 先 `--dry-run`；检查同目录 mp4 与 mp3 文件名前 4 词是否一致。

**Q：配音太慢 / 念不完？**  
A：已启用 1.35× 提速 + YAML 本地压缩；仍超长的句才送 Gemini（若开启优化）。

**Q：Mac 和 Windows 配置要两份吗？**  
A：`tubeget.config.json` 和 yaml 策略可共用同一份；拷过去改路径即可。

---

## 七、文件清单（便携 merge_dub_tool）

```
merge_dub_tool/
├── merge_dub_videos.py
├── merge_dub_videos.sh
├── 合并配音视频.bat          # 仅 Windows
├── 使用说明.txt
└── Mac使用说明.md            # 本文件
```

完整配音流水线见 Windows 侧 `components/` 目录。
