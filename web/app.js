const intensityControls = [
  ["guard_density", "Guard density"],
  ["enemy_toughness", "Enemy toughness"],
  ["supplies", "Supplies"],
  ["treasure", "Treasure"],
  ["secrets", "Secrets"],
  ["locked_doors", "Locked doors"],
  ["layout_complexity", "Layout complexity"],
  ["decoration_amount", "Decoration amount"],
  ["room_shape_variation", "Room shape variation"],
  ["patrol_activity", "Patrol activity"],
  ["atmosphere", "Atmosphere"],
  ["secret_reward_quality", "Secret reward quality"],
];

const form = document.querySelector("#generator-form");
const checkerForm = document.querySelector("#checker-form");
const controls = document.querySelector("#intensity-controls");
const generateButton = document.querySelector("#generate-button");
const cancelButton = document.querySelector("#cancel-button");
const checkButton = document.querySelector("#check-button");
const progress = document.querySelector("#generation-progress");
const generationStatus = document.querySelector("#generation-status");
const checkerStatus = document.querySelector("#checker-status");
const checkerResult = document.querySelector("#checker-result");
const buildLabel = document.querySelector("#build-label");

let worker;
let busy = null;
let runtimeReady = false;

function addIntensityControls() {
  for (const [name, label] of intensityControls) {
    const wrapper = document.createElement("label");
    wrapper.className = "slider-label";
    wrapper.htmlFor = name;

    const title = document.createElement("span");
    title.textContent = label;

    const value = document.createElement("output");
    value.className = "slider-value";
    value.htmlFor = name;
    value.textContent = "3";

    const input = document.createElement("input");
    input.id = name;
    input.name = name;
    input.type = "range";
    input.min = "1";
    input.max = "5";
    input.value = "3";
    input.addEventListener("input", () => {
      value.textContent = input.value;
    });

    wrapper.append(title, value, input);
    controls.append(wrapper);
  }
}

function setBusy(operation) {
  busy = operation;
  generateButton.disabled = operation !== null;
  checkButton.disabled = operation !== null;
  cancelButton.disabled = operation !== "generate";
}

function setGenerationStatus(message, isError = false) {
  generationStatus.textContent = message;
  generationStatus.classList.toggle("error", isError);
}

function setCheckerStatus(message, className = "") {
  checkerStatus.textContent = message;
  checkerStatus.className = `operation-status ${className}`.trim();
}

function createWorker() {
  if (worker) {
    worker.terminate();
  }
  runtimeReady = false;
  worker = new Worker("./worker.js", { type: "module" });
  worker.addEventListener("message", handleWorkerMessage);
  worker.addEventListener("error", (event) => {
    setBusy(null);
    setGenerationStatus(`Worker error: ${event.message}`, true);
    setCheckerStatus(`Worker error: ${event.message}`, "error");
  });
}

function handleWorkerMessage({ data }) {
  switch (data.type) {
    case "runtime-status":
      if (!busy) {
        setGenerationStatus(data.message);
      }
      break;
    case "ready":
      runtimeReady = true;
      buildLabel.textContent = `InfiniWolf ${data.build} · main ${data.commit.slice(0, 7)} · Pyodide ${data.pyodide}`;
      if (!busy) {
        setGenerationStatus("Ready. Generation runs in a background worker and may take several minutes.");
      }
      break;
    case "progress":
      progress.value = data.floor;
      setGenerationStatus(`Generated floor ${data.floor} of ${data.total}.`);
      break;
    case "generated":
      finishDownload(data);
      break;
    case "checked":
      showCheckResult(data.result);
      setBusy(null);
      break;
    case "error":
      setBusy(null);
      if (data.operation === "check") {
        setCheckerStatus(data.message, "error");
        checkerResult.hidden = true;
      } else {
        setGenerationStatus(data.message, true);
      }
      break;
    default:
      console.warn("Unknown worker message", data);
  }
}

function generationSettings() {
  const settings = {
    seed: document.querySelector("#seed").value,
    generation_quality: document.querySelector("#generation-quality").value,
    theme_bias: document.querySelector("#theme-bias").value,
  };
  for (const [name] of intensityControls) {
    settings[name] = Number(document.querySelector(`#${name}`).value);
  }
  return settings;
}

function finishDownload({ bytes, metadata }) {
  const blob = new Blob([bytes], { type: "application/zip" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `infiniwolf-${metadata.seed}.pk3`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);

  progress.value = 10;
  setGenerationStatus(`Generated seed ${metadata.seed} with InfiniWolf ${metadata.build}. Download started.`);
  setBusy(null);
}

function showCheckResult(result) {
  const count = result.maps_checked;
  const summary = `${result.verdict}: ${result.watermark_floors}/${count} primary, ${result.secondary_floors}/${count} secondary, ${result.structural_floors}/${count} structural, global42=${result.global_42}`;
  setCheckerStatus(summary, result.verdict);
  checkerResult.textContent = JSON.stringify(result, null, 2);
  checkerResult.hidden = false;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy("generate");
  progress.value = 0;
  setGenerationStatus(runtimeReady ? "Starting campaign generation…" : "Loading Python before generation…");
  worker.postMessage({ type: "generate", settings: generationSettings() });
});

cancelButton.addEventListener("click", () => {
  if (busy !== "generate") return;
  createWorker();
  setBusy(null);
  progress.value = 0;
  setGenerationStatus("Generation cancelled. Reloading the Python runtime…");
});

checkerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;

  const file = document.querySelector("#check-file").files[0];
  if (!file) {
    setCheckerStatus("Choose a PK3 or WAD first.", "error");
    return;
  }

  const floorText = document.querySelector("#check-floor").value.trim();
  const floor = floorText ? Number(floorText) : null;
  if (floor !== null && (!Number.isInteger(floor) || floor < 1 || floor > 10)) {
    setCheckerStatus("Standalone floor must be between 1 and 10.", "error");
    return;
  }

  setBusy("check");
  checkerResult.hidden = true;
  setCheckerStatus(runtimeReady ? "Checking map planes…" : "Loading Python before checking…");
  const bytes = await file.arrayBuffer();
  worker.postMessage({ type: "check", name: file.name, floor, bytes }, [bytes]);
});

addIntensityControls();
createWorker();
