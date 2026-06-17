# Sync latest components scripts to dubMAC (for Mac copy)
# Usage: powershell -File sync_to_dubMAC.ps1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src = Join-Path $Root 'components'
$Dst = Join-Path $Root 'dubMAC'
$DstComp = Join-Path $Dst 'components'
$DstMerge = Join-Path $Dst 'merge_dub_tool'

New-Item -ItemType Directory -Force -Path $DstComp, $DstMerge | Out-Null

$pyFiles = @(
    'run_batch.py', 'run_batch_course.py', 'step1_subtitle_to_audio.py',
    'step2_merge_video.py', 'batch_report.py', 'edge_tts_scheduler.py',
    'edge_segment_scheduler.py', 'cue_synth_eligibility.py',
    'diagnose_edge_tts.py', 'test_edge_segment.py', 'dub_naming.py',
    'ai_shorten.py', 'zh_compress.py', 'analyze_subtitles.py', 'tts_metrics.py',
    'preview_local_voices.py', 'list_local_voices.py', 'check_env.py',
    'speech_activity.py', 'speech_grouping.py', 'gemini_secrets.py',
    'progress_log.py', 'terminology.py',
    'merge_dub_videos.py',
    'notebooklm_dub_automation.py', 'quick_bilingual_dub.py', 'make_study_srt.py',
    'dub_translate_prompts.py', 'dub_voice_prefs.py'
)
$configFiles = @(
    'requirements.txt', 'tubeget.config.json', 'tubeget.config.edge.example.json',
    'gemini-code-1780751633188.yaml', 'zh_strategy.example.json',
    'merge_dub_videos.sh',
    'notebooklm_dub_prompt.txt'
)
foreach ($f in ($pyFiles + $configFiles)) {
    $srcPath = Join-Path $Src $f
    if (-not (Test-Path -LiteralPath $srcPath)) {
        Write-Warning "Skip missing: $f"
        continue
    }
    Copy-Item -LiteralPath $srcPath -Destination (Join-Path $DstComp $f) -Force
}

foreach ($item in Get-ChildItem -LiteralPath $Src -File) {
    if ($item.Name -like 'notebooklm_*prompt*' -and $item.Extension -eq '.txt') {
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $DstComp $item.Name) -Force
    }
    if ($item.Name -like 'Mac*.md') {
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $DstComp $item.Name) -Force
    }
}

$mergeSrc = Join-Path $Src 'merge_dub_tool'
foreach ($item in Get-ChildItem -LiteralPath $mergeSrc -File) {
    if ($item.Extension -notin '.py', '.sh', '.txt', '.md') { continue }
    if ($item.Name -like '*成功*') { continue }
    Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $DstMerge $item.Name) -Force
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "OK dubMAC synced at $stamp"
Write-Host "Mac shells in dubMAC/: dub_local.sh, notebooklm_dub_translate.sh, quick_bilingual_dub.sh"
