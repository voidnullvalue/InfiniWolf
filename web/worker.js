import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.3/full/pyodide.mjs";

const PYODIDE_INDEX = "https://cdn.jsdelivr.net/pyodide/v314.0.3/full/";
const BUILD_URL = new URL("./build.json", import.meta.url);
const GENERATED_PATH = "/tmp/infiniwolf.pk3";

function errorMessage(error) {
  return error?.message || String(error);
}

async function initialize() {
  self.postMessage({ type: "runtime-status", message: "Loading CPython WebAssembly…" });
  const buildResponse = await fetch(BUILD_URL, { cache: "no-store" });
  if (!buildResponse.ok) {
    throw new Error(`Could not load build metadata (${buildResponse.status}).`);
  }
  const build = await buildResponse.json();

  const pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX });
  self.postMessage({ type: "runtime-status", message: "Installing the current InfiniWolf main wheel…" });
  await pyodide.loadPackage("micropip");
  const micropip = pyodide.pyimport("micropip");
  try {
    const wheelUrl = new URL(build.wheel, BUILD_URL).href;
    await micropip.install(wheelUrl);
  } finally {
    micropip.destroy();
  }

  const pythonBuild = pyodide.runPython(`
from infiniwolf.build_info import build_label
build_label()
`);
  self.postMessage({
    type: "ready",
    build: pythonBuild,
    commit: build.commit,
    pyodide: pyodide.version,
  });
  return pyodide;
}

const runtime = initialize();
runtime.catch((error) => {
  self.postMessage({ type: "error", operation: "runtime", message: errorMessage(error) });
});

async function generate(pyodide, settings) {
  const progressCallback = (floor, total) => {
    self.postMessage({ type: "progress", floor, total });
  };
  pyodide.globals.set("web_settings_json", JSON.stringify(settings));
  pyodide.globals.set("web_progress_callback", progressCallback);
  try {
    const metadataJson = await pyodide.runPythonAsync(`
from infiniwolf.web import generate_for_web
generate_for_web(web_settings_json, "${GENERATED_PATH}", web_progress_callback)
`);
    const data = pyodide.FS.readFile(GENERATED_PATH);
    const buffer = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
    self.postMessage({
      type: "generated",
      bytes: buffer,
      metadata: JSON.parse(metadataJson),
    }, [buffer]);
  } finally {
    pyodide.globals.delete("web_settings_json");
    pyodide.globals.delete("web_progress_callback");
    try {
      pyodide.FS.unlink(GENERATED_PATH);
    } catch (error) {
      if (!String(error).includes("No such file")) throw error;
    }
  }
}

async function check(pyodide, name, floor, buffer) {
  const suffix = name.toLowerCase().endsWith(".wad") ? ".wad" : ".pk3";
  const path = `/tmp/infiniwolf-upload${suffix}`;
  pyodide.FS.writeFile(path, new Uint8Array(buffer));
  pyodide.globals.set("web_check_path", path);
  pyodide.globals.set("web_check_floor", floor);
  try {
    const resultJson = await pyodide.runPythonAsync(`
from infiniwolf.web import check_for_web
check_for_web(web_check_path, web_check_floor)
`);
    self.postMessage({ type: "checked", result: JSON.parse(resultJson) });
  } finally {
    pyodide.globals.delete("web_check_path");
    pyodide.globals.delete("web_check_floor");
    pyodide.FS.unlink(path);
  }
}

self.onmessage = async ({ data }) => {
  try {
    const pyodide = await runtime;
    if (data.type === "generate") {
      await generate(pyodide, data.settings);
    } else if (data.type === "check") {
      await check(pyodide, data.name, data.floor, data.bytes);
    } else {
      throw new Error(`Unknown operation: ${data.type}`);
    }
  } catch (error) {
    self.postMessage({
      type: "error",
      operation: data.type,
      message: errorMessage(error),
    });
  }
};
