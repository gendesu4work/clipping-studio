// Creator Studio desktop app.
// Runs the auto-updater, starts the Python server (server.py) hidden in the background,
// shows a splash while it boots, then loads the app in a native window.
// Closing the window stops the server it started.
const { app, BrowserWindow, shell, dialog, Menu } = require("electron");
const { spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");

// Dev (npm start): run the Python code in this repo.
// Installed app: the installer ships the Python code in resources/studio; it is copied to a
// writable per-user folder (%LOCALAPPDATA%\Creator Studio\app) so the auto-updater and your
// settings/library survive reinstalls and upgrades.
const BUNDLED = app.isPackaged ? path.join(process.resourcesPath, "studio") : null;
const ROOT = app.isPackaged
  ? path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "Creator Studio", "app")
  : path.join(__dirname, "..");
const PORT = 7860;
const URL = `http://127.0.0.1:${PORT}`;
const ICON = app.isPackaged ? path.join(process.resourcesPath, "studio", "studio.ico") : path.join(ROOT, "studio.ico");

let win = null;
let server = null; // the server process this app started (null if one was already running)

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });
  app.whenReady().then(main);
}

app.setAppUserModelId("CreatorStudio.App");

function pythonExe() {
  const venv = path.join(ROOT, ".venv", "Scripts", "python.exe");
  return fs.existsSync(venv) ? venv : "python";
}

// Installed app only: copy the bundled code into ROOT on first run and after an upgrade.
// Only the shipped code files are copied, so personal data in ROOT is never touched.
function installCode() {
  if (!BUNDLED) return;
  const stamp = path.join(ROOT, ".installed_version");
  let have = null;
  try { have = fs.readFileSync(stamp, "utf8").trim(); } catch {}
  if (have === app.getVersion() && fs.existsSync(path.join(ROOT, "server.py"))) return;
  fs.mkdirSync(ROOT, { recursive: true });
  fs.cpSync(BUNDLED, ROOT, { recursive: true, force: true });
  fs.writeFileSync(stamp, app.getVersion());
  log(`installed bundled code v${app.getVersion()} into ${ROOT}`);
}

function findSystemPython() {
  for (const [cmd, args] of [["py", ["-3"]], ["python", []], ["python3", []]]) {
    const r = spawnSync(cmd, [...args, "--version"], { windowsHide: true, encoding: "utf8" });
    if (r.status === 0 && /Python 3\.(1\d|[2-9]\d)/.test((r.stdout || "") + (r.stderr || ""))) return [cmd, args];
  }
  return null;
}

function run(cmd, args) {
  return new Promise(resolve => {
    const logFd = fs.openSync(path.join(ROOT, "studio.log"), "a");
    const p = spawn(cmd, args, { cwd: ROOT, stdio: ["ignore", logFd, logFd], windowsHide: true });
    p.on("error", e => { log(`${cmd} failed: ${e}`); resolve(false); });
    p.on("exit", code => resolve(code === 0));
  });
}

// Installed app only: create .venv and install requirements the first time.
async function ensureVenv() {
  if (!BUNDLED) return true;
  const venvPy = path.join(ROOT, ".venv", "Scripts", "python.exe");
  const ready = path.join(ROOT, ".venv", ".ready");
  if (fs.existsSync(ready)) return true;
  const py = findSystemPython();
  if (!py) {
    const r = dialog.showMessageBoxSync({
      type: "error", title: "Creator Studio", buttons: ["Get Python", "Close"],
      message: "Creator Studio needs Python 3.10 or newer.",
      detail: "Install Python from python.org (tick \"Add python.exe to PATH\"), then open Creator Studio again.",
    });
    if (r === 0) shell.openExternal("https://www.python.org/downloads/");
    return false;
  }
  if (win) await win.loadURL(splashHtml("First-time setup: creating Python environment…"));
  if (!fs.existsSync(venvPy) && !(await run(py[0], [...py[1], "-m", "venv", ".venv"]))) return false;
  if (win) await win.loadURL(splashHtml("First-time setup: installing packages (this can take several minutes)…"));
  if (!(await run(venvPy, ["-m", "pip", "install", "-q", "-r", "requirements.txt"]))) return false;
  // Tell updater.py these requirements are installed so it doesn't reinstall them right away.
  const reqHash = crypto.createHash("sha256").update(fs.readFileSync(path.join(ROOT, "requirements.txt"))).digest("hex");
  fs.writeFileSync(path.join(ROOT, ".installed_requirements"), reqHash);
  // ffmpeg isn't a pip package: fetch a private copy now (skipped if it's already on PATH).
  if (win) await win.loadURL(splashHtml("First-time setup: downloading FFmpeg…"));
  await run(venvPy, ["-c", "import ffmpeg_tools, sys; sys.exit(0 if ffmpeg_tools.ensure_ffmpeg() else 1)"]);
  fs.writeFileSync(ready, new Date().toISOString());
  return true;
}

function log(msg) {
  try { fs.appendFileSync(path.join(ROOT, "studio.log"), `[electron] ${msg}\n`); } catch {}
}

function serverUp() {
  return new Promise(resolve => {
    const req = http.get(URL + "/api/library", { timeout: 1000 }, res => { res.resume(); resolve(true); });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

function runUpdater() {
  // Same updater the old launcher used; never let it block the app from opening.
  try {
    const r = spawnSync(pythonExe(), ["updater.py"], { cwd: ROOT, timeout: 600000, windowsHide: true, encoding: "utf8" }); // may pip-install new packages
    return (r.stdout || "").includes("updated") && !(r.stdout || "").includes("already");
  } catch (e) {
    log(`updater failed: ${e}`);
    return false;
  }
}

function stopPortOwner() {
  // Stop whatever server is listening on the port (used when an update needs a restart).
  const out = spawnSync("netstat", ["-ano", "-p", "TCP"], { encoding: "utf8", windowsHide: true }).stdout || "";
  const pids = new Set(out.split(/\r?\n/)
    .filter(l => l.includes(`127.0.0.1:${PORT}`) && l.includes("LISTENING"))
    .map(l => l.trim().split(/\s+/).pop()));
  for (const pid of pids) spawnSync("taskkill", ["/PID", pid, "/T", "/F"], { windowsHide: true });
}

function startServer() {
  const logFd = fs.openSync(path.join(ROOT, "studio.log"), "a");
  server = spawn(pythonExe(), ["server.py"], {
    cwd: ROOT,
    env: { ...process.env, NO_BROWSER: "1", PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", logFd, logFd],
    windowsHide: true,
  });
  server.on("error", e => log(`server failed to start: ${e}`));
  server.on("exit", code => { log(`server exited (${code})`); server = null; });
}

function stopServer() {
  if (server && server.pid) {
    spawnSync("taskkill", ["/PID", String(server.pid), "/T", "/F"], { windowsHide: true });
    server = null;
  }
}

function splashHtml(text) {
  return "data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><html><body style="
    margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
    background:#0e0f13;color:#d8dbe2;font:15px system-ui,Segoe UI,sans-serif">
    <div style="width:28px;height:28px;border:3px solid #333;border-top-color:#e8e8e8;border-radius:50%;
      animation:s 0.8s linear infinite"></div>
    <p style="margin-top:18px;opacity:.8">${text}</p>
    <style>@keyframes s{to{transform:rotate(360deg)}}</style></body></html>`);
}

async function main() {
  Menu.setApplicationMenu(null);
  win = new BrowserWindow({
    width: 1320, height: 900, minWidth: 900, minHeight: 600,
    title: "Creator Studio", icon: ICON, backgroundColor: "#0e0f13", show: false,
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.once("ready-to-show", () => win.show());
  win.on("closed", () => { win = null; });

  // Links to other sites (Discord, YouTube, ...) open in the normal browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(URL)) { shell.openExternal(url); return { action: "deny" }; }
    return { action: "allow" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(URL) && !url.startsWith("data:")) { e.preventDefault(); shell.openExternal(url); }
  });
  // Keyboard shortcuts the menu bar would normally give.
  win.webContents.on("before-input-event", (e, input) => {
    if (input.type !== "keyDown") return;
    if (input.key === "F5" || (input.control && input.key.toLowerCase() === "r")) win.webContents.reload();
    if (input.key === "F12" || (input.control && input.shift && input.key.toLowerCase() === "i")) win.webContents.toggleDevTools();
  });

  try {
    installCode();
  } catch (e) {
    dialog.showErrorBox("Creator Studio", `Couldn't set up the app folder:\n${e}`);
    app.quit();
    return;
  }
  if (!(await ensureVenv())) {
    if (fs.existsSync(path.join(ROOT, "studio.log")) && findSystemPython())
      dialog.showErrorBox("Creator Studio", `Setup failed. See studio.log in:\n${ROOT}`);
    app.quit();
    return;
  }

  await win.loadURL(splashHtml("Checking for updates…"));
  const updated = await new Promise(r => setImmediate(() => r(runUpdater())));

  if (updated && await serverUp()) stopPortOwner(); // restart old server on the new code
  if (!(await serverUp())) {
    if (win) await win.loadURL(splashHtml("Starting Creator Studio…"));
    startServer();
    let up = false;
    for (let i = 0; i < 240 && !up; i++) { // first start loads ML libraries, allow up to 2 minutes
      if (!server) break;
      await sleep(500);
      up = await serverUp();
    }
    if (!up) {
      dialog.showErrorBox("Creator Studio", `The server didn't start. Check studio.log in:\n${ROOT}\n\n` +
        "Make sure Python and the requirements are installed (pip install -r requirements.txt).");
      app.quit();
      return;
    }
  }
  if (win) win.loadURL(URL);
}

app.on("window-all-closed", () => app.quit());
app.on("will-quit", stopServer);
