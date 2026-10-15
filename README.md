# NotebookLM Dub Workflow

Translate local videos with **NotebookLM's free quota**, then dub them for study — especially AI and academic talks.

用 NotebookLM 免费额度翻译本地视频并配音，方便学习 AI / 学术类内容。

## Two routes / 两条路线

- **Route A (free, recommended for study)**  
  NotebookLM 译字幕 → `quick_bilingual_dub.bat`  
  不调用 Gemini API。

- **Route B (optional commercial check)**  
  `fit_translate.bat`（唯一一次 Gemini）→ `commercial_dub_test.bat`（只做 TTS）

See `WORKFLOWS.txt` for the cost policy.

## API keys

This repo does **not** contain API keys. Gemini (route B only) is read from the Windows user environment variable `GEMINI_API_KEY`.  
See `components/GEMINI_API_KEY_SETUP.txt`.

本仓库不含密钥。路线 B 才需要在 Windows 用户环境变量中设置 `GEMINI_API_KEY`。

## License

MIT
