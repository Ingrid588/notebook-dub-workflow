/**
 * Manifest V3 Service Worker — 音量持久化与消息广播
 */

const DEFAULT_VOLUME = 1.0;
const STORAGE_KEY = "volume";

/** @type {number} */
let currentVolume = DEFAULT_VOLUME;

async function loadVolume() {
  const result = await chrome.storage.local.get(STORAGE_KEY);
  const saved = result[STORAGE_KEY];
  if (typeof saved === "number" && saved >= 0 && saved <= 1) {
    currentVolume = saved;
  }
  return currentVolume;
}

async function setVolume(volume) {
  const clamped = Math.min(1, Math.max(0, volume));
  currentVolume = clamped;
  await chrome.storage.local.set({ [STORAGE_KEY]: clamped });
  await broadcastVolume(clamped);
}

async function broadcastVolume(volume) {
  const tabs = await chrome.tabs.query({ url: "*://*.youtube.com/*" });
  await Promise.all(
    tabs.map(async (tab) => {
      if (!tab.id) return;
      try {
        await chrome.tabs.sendMessage(tab.id, { type: "SET_VOLUME", volume });
      } catch {
        // content script 未就绪
      }
    })
  );
}

chrome.runtime.onInstalled.addListener(async () => {
  await loadVolume();
  console.log("[YT SoftMix] installed, volume:", currentVolume);
});

chrome.runtime.onStartup.addListener(loadVolume);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "GET_VOLUME") {
    sendResponse({ volume: currentVolume });
    return false;
  }

  if (message?.type === "SET_VOLUME" && typeof message.volume === "number") {
    setVolume(message.volume)
      .then(() => sendResponse({ ok: true, volume: currentVolume }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (message?.type === "CONTENT_READY" && sender.tab?.id) {
    chrome.tabs
      .sendMessage(sender.tab.id, { type: "SET_VOLUME", volume: currentVolume })
      .catch(() => {});
    sendResponse({ ok: true });
    return false;
  }

  return false;
});

loadVolume();
