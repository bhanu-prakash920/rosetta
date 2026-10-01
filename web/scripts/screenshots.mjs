// Drives the installed Chrome through the console and saves one screenshot per page.
// Usage: node scripts/screenshots.mjs [baseUrl] [outDir]
import fs from "node:fs";
import path from "node:path";
import puppeteer from "puppeteer-core";

const base = process.argv[2] ?? "http://127.0.0.1:8000";
const out = process.argv[3] ?? "../docs/screenshots";
const password = process.env.ROSETTA_DEMO_PASSWORD ?? "rosetta-demo-2026";
const user = process.env.SHOT_USER ?? "engineer@rosetta.example";
const only = (process.env.SHOT_ONLY ?? "").split(",").filter(Boolean);
const chrome = process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
fs.mkdirSync(out, { recursive: true });

const pages = [
  ["landing", "/", false],
  ["login", "/login", false],
  ["live", "/app", true],
  ["sources", "/app/sources", true],
  ["source-detail", "/app/sources/pacifica", true],
  ["dead-letters", "/app/dead-letters", true],
  ["studio", "/app/studio/helix", true],
  ["fleet", "/app/fleet", true],
  ["insights", "/app/insights", true],
  ["compliance", "/app/compliance", true],
];

const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ["--no-sandbox", "--hide-scrollbars"] });
const page = await browser.newPage();
await page.setViewport({ width: 1480, height: 960, deviceScaleFactor: 1 });
const errors = [];
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("console", (m) => { if (m.type() === "error") errors.push(`console: ${m.text().slice(0, 200)}`); });
let signedIn = false;
for (const [name, url, auth] of pages) {
  if (only.length && !only.includes(name)) continue;
  if (auth && !signedIn) {
    await page.goto(base + "/login", { waitUntil: "networkidle2" });
    await page.type("#email", user);
    await page.type("#pw", password);
    await Promise.all([page.waitForFunction(() => location.pathname.startsWith("/app"), { timeout: 15000 }), page.click("button.btn.flame")]);
    signedIn = true;
  }
  await page.goto(base + url, { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, Number(process.env.SHOT_WAIT ?? 3500)));
  const full = process.env.SHOT_FULL !== "0";
  await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: full });
  console.log("saved", name);
}
await browser.close();
if (errors.length) { console.log("browser errors:"); for (const e of [...new Set(errors)].slice(0, 20)) console.log("  " + e); }
