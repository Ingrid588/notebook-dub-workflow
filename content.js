/**
 * Content Script — 影子调音 + 纯前端配音轨道混音（零 Web Audio 劫持）
 *
 * 原视频始终走浏览器原生播放轨道，立体声 / BGM 不被污染。
 * 闪避（Soft Ducking）仅通过 video.volume 实现；中文配音由独立 Audio 实例播放。
 */

const DUCK_RATIO = 0.15;
const DEFAULT_DUBBING_DURATION_SEC = 5;
const VOLUME_STORAGE_SYNC_MS = 500;

/** @type {number} */
let userVolume = 1.0;

/** @type {boolean} */
let isDucking = false;

/** @type {number | null} */
let volumeBeforeDuck = null;

/** @type {HTMLAudioElement | null} */
let dubbingPlayer = null;

/** @type {boolean} */
let applyingVolume = false;

/** @type {number | null} */
let volumeSyncTimer = null;

// ---------------------------------------------------------------------------
// Video 定位
// ---------------------------------------------------------------------------

/**
 * @returns {HTMLVideoElement | null}
 */
function getMainPlayerVideo() {
  try {
    const explicit = document.querySelector(
      "#movie_player video.html5-main-video, video.html5-main-video"
    );
    if (explicit instanceof HTMLVideoElement) return explicit;

    const candidates = [
      ...document.querySelectorAll("#movie_player video, .html5-video-player video"),
    ].filter((node) => node instanceof HTMLVideoElement);

    if (candidates.length === 0) return null;

    return (
      candidates
        .filter((v) => v.clientWidth > 0 && v.clientHeight > 0)
        .sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0] ??
      null
    );
  } catch (err) {
    console.warn("[YT SoftMix] getMainPlayerVideo failed:", err);
    return null;
  }
}

/**
 * @param {number} volume
 * @returns {number}
 */
function clampVolume(volume) {
  return Math.min(1, Math.max(0, Number(volume) || 0));
}

/**
 * @param {HTMLVideoElement} video
 * @param {number} volume
 */
function writeVideoVolume(video, volume) {
  try {
    applyingVolume = true;
    video.volume = clampVolume(volume);
  } catch (err) {
    console.warn("[YT SoftMix] writeVideoVolume failed:", err);
  } finally {
    applyingVolume = false;
  }
}

/**
 * 从 video 同步用户基准音量（用户通过 YouTube 播放器调音量时）
 * @param {HTMLVideoElement} video
 */
function syncUserVolumeFromVideo(video) {
  if (applyingVolume || isDucking) return;
  userVolume = clampVolume(video.volume);
}

/**
 * @param {HTMLVideoElement} video
 */
function bindVideoVolumeListener(video) {
  try {
    video.addEventListener("volumechange", () => {
      syncUserVolumeFromVideo(video);
    });
  } catch (err) {
    console.warn("[YT SoftMix] bindVideoVolumeListener failed:", err);
  }
}

// ---------------------------------------------------------------------------
// 软闪避 Soft Ducking
// ---------------------------------------------------------------------------

/**
 * 开始软闪避：原视频音量 → userVolume × 15%
 * @param {HTMLVideoElement} [video]
 * @returns {boolean}
 */
function startSoftDuck(video = getMainPlayerVideo()) {
  try {
    if (!video) {
      console.warn("[YT SoftMix] startSoftDuck: no video");
      return false;
    }

    if (isDucking) return true;

    volumeBeforeDuck = clampVolume(video.volume);
    userVolume = volumeBeforeDuck;

    const ducked = clampVolume(userVolume * DUCK_RATIO);
    writeVideoVolume(video, ducked);
    isDucking = true;

    console.log("[YT SoftMix] Soft duck ON", { userVolume, ducked });
    return true;
  } catch (err) {
    console.warn("[YT SoftMix] startSoftDuck failed:", err);
    return false;
  }
}

/**
 * 结束软闪避：恢复原视频音量
 * @param {HTMLVideoElement} [video]
 * @returns {boolean}
 */
function endSoftDuck(video = getMainPlayerVideo()) {
  try {
    if (!video) return false;

    const restore =
      volumeBeforeDuck !== null ? volumeBeforeDuck : clampVolume(userVolume);

    writeVideoVolume(video, restore);
    userVolume = restore;
    isDucking = false;
    volumeBeforeDuck = null;

    console.log("[YT SoftMix] Soft duck OFF", { restored: restore });
    return true;
  } catch (err) {
    console.warn("[YT SoftMix] endSoftDuck failed:", err);
    return false;
  }
}

// ---------------------------------------------------------------------------
// 独立配音播放器
// ---------------------------------------------------------------------------

/**
 * 停止并清理配音 Audio
 */
function stopDubbing() {
  try {
    if (!dubbingPlayer) return;

    const player = dubbingPlayer;
    dubbingPlayer = null;

    player.onplay = null;
    player.onended = null;
    player.onerror = null;

    const blobUrl = player.src?.startsWith("blob:") ? player.src : null;
    player.pause();
    player.removeAttribute("src");
    player.load();
    if (blobUrl) URL.revokeObjectURL(blobUrl);
  } catch (err) {
    console.warn("[YT SoftMix] stopDubbing failed:", err);
  }
}

/**
 * 生成测试配音 WAV Blob URL
 * @param {number} durationSec
 * @returns {string}
 */
function createTestDubbingUrl(durationSec) {
  const sampleRate = 44100;
  const length = Math.floor(sampleRate * durationSec);
  const samples = new Float32Array(length);
  const baseFreq = 440;

  for (let i = 0; i < length; i++) {
    const t = i / sampleRate;
    const attack = Math.min(1, t / 0.05);
    const release = Math.min(1, (durationSec - t) / 0.08);
    const envelope = Math.min(attack, release);
    samples[i] = Math.sin(2 * Math.PI * baseFreq * t) * 0.3 * envelope;
  }

  const dataSize = samples.length * 2;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  const writeStr = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };

  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s * 0x7fff, true);
    offset += 2;
  }

  return URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
}

/**
 * 播放配音 URL，并同步软闪避原视频
 * @param {string} audioUrl
 * @returns {Promise<void>}
 */
async function playDubbingTrack(audioUrl) {
  const video = getMainPlayerVideo();
  if (!video) {
    console.warn("[YT SoftMix] playDubbingTrack: no video");
    return;
  }

  stopDubbing();
  startSoftDuck(video);

  const player = new Audio(audioUrl);
  dubbingPlayer = player;
  player.volume = 1;

  const cleanup = () => {
    if (dubbingPlayer === player) dubbingPlayer = null;
    endSoftDuck(video);
  };

  player.addEventListener("play", () => {
    startSoftDuck(video);
  });

  player.addEventListener("ended", () => {
    cleanup();
    if (audioUrl.startsWith("blob:")) URL.revokeObjectURL(audioUrl);
    console.log("[YT SoftMix] Dubbing ended, volume restored.");
  });

  player.addEventListener("error", () => {
    cleanup();
    if (audioUrl.startsWith("blob:")) URL.revokeObjectURL(audioUrl);
    console.warn("[YT SoftMix] Dubbing playback error.");
  });

  try {
    await player.play();
  } catch (err) {
    cleanup();
    throw err;
  }
}

/**
 * 模拟一段中文配音（测试用）
 * @param {number} [duration=5]
 * @returns {Promise<void>}
 */
async function simulateIncomingDubbing(duration = DEFAULT_DUBBING_DURATION_SEC) {
  const durationSec = Math.max(0.5, Number(duration) || DEFAULT_DUBBING_DURATION_SEC);
  const url = createTestDubbingUrl(durationSec);
  await playDubbingTrack(url);
  console.log(`[YT SoftMix] Simulating dubbing ${durationSec}s`);
}

// ---------------------------------------------------------------------------
// 音量控制（扩展滑块 → 原生 video.volume）
// ---------------------------------------------------------------------------

/**
 * @param {number} volume 0 ~ 1
 */
function setUserVolume(volume) {
  userVolume = clampVolume(volume);

  const video = getMainPlayerVideo();
  if (!video) return;

  if (isDucking) {
    writeVideoVolume(video, clampVolume(userVolume * DUCK_RATIO));
  } else {
    writeVideoVolume(video, userVolume);
  }
}

// ---------------------------------------------------------------------------
// 生命周期 destroy()
// ---------------------------------------------------------------------------

/**
 * 插件关闭 / 页面卸载时清理：停止配音，恢复 video.volume = 1.0
 */
function destroy() {
  try {
    stopDubbing();

    const video = getMainPlayerVideo();
    if (video) {
      isDucking = false;
      volumeBeforeDuck = null;
      userVolume = 1.0;
      writeVideoVolume(video, 1.0);
    }

    if (volumeSyncTimer !== null) {
      clearInterval(volumeSyncTimer);
      volumeSyncTimer = null;
    }

    console.log("[YT SoftMix] destroy() complete — native volume restored to 1.0");
  } catch (err) {
    console.warn("[YT SoftMix] destroy failed:", err);
  }
}

function isExtensionAlive() {
  try {
    return Boolean(chrome.runtime?.id);
  } catch {
    return false;
  }
}

function startExtensionWatchdog() {
  if (volumeSyncTimer !== null) return;

  volumeSyncTimer = window.setInterval(() => {
    if (!isExtensionAlive()) {
      destroy();
      if (volumeSyncTimer !== null) {
        clearInterval(volumeSyncTimer);
        volumeSyncTimer = null;
      }
    }
  }, VOLUME_STORAGE_SYNC_MS);
}

// ---------------------------------------------------------------------------
// 消息 / 初始化
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  try {
    if (message?.type === "SET_VOLUME" && typeof message.volume === "number") {
      setUserVolume(message.volume);
      sendResponse({ ok: true, volume: userVolume, isDucking });
      return false;
    }

    if (message?.type === "GET_STATUS" || message?.type === "GET_GRAPHS") {
      const video = getMainPlayerVideo();
      sendResponse({
        ok: true,
        mode: "native-soft-duck",
        volume: userVolume,
        videoVolume: video?.volume ?? null,
        isDucking,
        dubbingActive: Boolean(dubbingPlayer),
        videoCount: video ? 1 : 0,
      });
      return false;
    }

    if (message?.type === "SIMULATE_DUBBING") {
      simulateIncomingDubbing(message.duration)
        .then(() => sendResponse({ ok: true }))
        .catch((err) => sendResponse({ ok: false, error: String(err) }));
      return true;
    }

    if (message?.type === "DESTROY" || message?.type === "RELEASE_AUDIO") {
      destroy();
      sendResponse({ ok: true });
      return false;
    }
  } catch (err) {
    sendResponse({ ok: false, error: String(err) });
  }

  return false;
});

function init() {
  try {
    const video = getMainPlayerVideo();
    if (video) {
      userVolume = clampVolume(video.volume);
      bindVideoVolumeListener(video);
    }

    startExtensionWatchdog();

    window.addEventListener("pagehide", destroy);
    window.addEventListener("beforeunload", destroy);

    chrome.runtime.sendMessage({ type: "CONTENT_READY" }).catch(() => {});
    console.log("[YT SoftMix] initialized (native audio, soft duck only)");
  } catch (err) {
    console.error("[YT SoftMix] init failed:", err);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

window.__ytVolumeController = {
  setVolume: setUserVolume,
  getVolume: () => userVolume,
  startSoftDuck,
  endSoftDuck,
  playDubbingTrack,
  simulateIncomingDubbing,
  stopDubbing,
  destroy,
  getState: () => {
    const video = getMainPlayerVideo();
    return {
      mode: "native-soft-duck",
      userVolume,
      videoVolume: video?.volume ?? null,
      isDucking,
      dubbingActive: Boolean(dubbingPlayer),
      duckRatio: DUCK_RATIO,
    };
  },
};
