const slider = document.getElementById("volumeSlider");
const volumeLabel = document.getElementById("volumeLabel");
const statusText = document.getElementById("statusText");
const statusBox = document.getElementById("status");
const testDuckBtn = document.getElementById("testDuckBtn");
const resetBtn = document.getElementById("resetBtn");

function formatPercent(volume) {
  return `${Math.round(volume * 100)}%`;
}

function setStatus(type, text) {
  statusBox.className = type ? `status ${type}` : "status";
  statusText.textContent = text;
}

function updateSlider(volume) {
  slider.value = String(Math.round(volume * 100));
  volumeLabel.textContent = formatPercent(volume);
}

async function getActiveYoutubeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url?.includes("youtube.com")) return null;
  return tab;
}

async function applyVolume(volume) {
  const response = await chrome.runtime.sendMessage({
    type: "SET_VOLUME",
    volume,
  });
  if (!response?.ok) throw new Error(response?.error ?? "设置音量失败");
  updateSlider(response.volume);
}

async function loadCurrentVolume() {
  const response = await chrome.runtime.sendMessage({ type: "GET_VOLUME" });
  if (typeof response?.volume === "number") updateSlider(response.volume);
}

async function checkActiveTab() {
  const tab = await getActiveYoutubeTab();
  if (!tab?.id) {
    setStatus("warn", "请先打开 YouTube 视频页。");
    return;
  }

  try {
    const response = await chrome.tabs.sendMessage(tab.id, { type: "GET_STATUS" });
    if (!response?.ok) {
      setStatus("warn", "页面尚未就绪，请刷新后重试。");
      return;
    }

    const duckHint = response.isDucking ? "（闪避中）" : "";
    setStatus(
      "ok",
      `原生音频模式${duckHint}。基准音量 ${formatPercent(response.volume ?? 0)}，当前 video.volume ${formatPercent(response.videoVolume ?? 0)}。`
    );
  } catch {
    setStatus("warn", "无法与页面通信，请刷新 YouTube 页面。");
  }
}

async function testDucking() {
  const tab = await getActiveYoutubeTab();
  if (!tab?.id) {
    setStatus("warn", "请先打开 YouTube 视频页。");
    return;
  }

  testDuckBtn.disabled = true;
  testDuckBtn.textContent = "播放中…";

  try {
    await chrome.tabs.sendMessage(tab.id, {
      type: "SIMULATE_DUBBING",
      duration: 5,
    });
    setStatus("ok", "正在测试：原视频降至 15%，5 秒测试配音播放中…");
    setTimeout(checkActiveTab, 5500);
  } catch (err) {
    setStatus("error", String(err));
  } finally {
    testDuckBtn.disabled = false;
    testDuckBtn.textContent = "测试闪避配音";
  }
}

async function resetAudio() {
  const tab = await getActiveYoutubeTab();
  if (!tab?.id) {
    setStatus("warn", "请先打开 YouTube 视频页。");
    return;
  }

  try {
    await chrome.tabs.sendMessage(tab.id, { type: "DESTROY" });
    setStatus("ok", "已重置：配音停止，video.volume 恢复为 100%。");
    updateSlider(1);
    await chrome.runtime.sendMessage({ type: "SET_VOLUME", volume: 1 });
  } catch (err) {
    setStatus("error", String(err));
  }
}

slider.addEventListener("input", () => {
  volumeLabel.textContent = `${slider.value}%`;
});

slider.addEventListener("change", async () => {
  try {
    await applyVolume(Number(slider.value) / 100);
    await checkActiveTab();
  } catch (err) {
    setStatus("error", String(err));
  }
});

document.querySelectorAll(".presets button").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await applyVolume(Number(button.dataset.volume));
      await checkActiveTab();
    } catch (err) {
      setStatus("error", String(err));
    }
  });
});

testDuckBtn.addEventListener("click", testDucking);
resetBtn.addEventListener("click", resetAudio);

loadCurrentVolume();
checkActiveTab();
