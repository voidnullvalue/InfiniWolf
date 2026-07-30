const { test, expect } = require("@playwright/test");

test("loads the current wheel and runs the map checker in Pyodide", async ({ page }) => {
  await page.goto("http://127.0.0.1:8000/", { waitUntil: "domcontentloaded" });

  const buildLabel = page.locator("#build-label");
  await expect(buildLabel).toContainText("InfiniWolf", { timeout: 180_000 });
  await expect(buildLabel).toContainText("Pyodide");

  await page.locator("#check-file").setInputFiles({
    name: "broken.wad",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("not a WDC map"),
  });
  await page.locator("#check-button").click();

  await expect(page.locator("#checker-status")).toContainText("not a WDC PWAD", {
    timeout: 30_000,
  });
});
