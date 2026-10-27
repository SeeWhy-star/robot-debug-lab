// Requires the lab server. Read-only checks: no robot commands or model calls.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
const { chromium } = createRequire(import.meta.url)("playwright");

export async function smoke(browser) {
  const output = fileURLToPath(new URL("../../.runtime/ui/", import.meta.url));
  await mkdir(output, { recursive: true });
  const errors = [];
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({ viewport });
    try {
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto("http://127.0.0.1:8765");
      await page.getByText("实时连接正常").waitFor({ state: "attached" });
      await page.getByRole("button", { name: "新建指令", exact: true }).click();
      assert.equal(await page.getByRole("button", { name: "解析并执行任务", exact: true }).isDisabled(), true);
      await page.getByRole("button", { name: "单机器人示例", exact: true }).click();
      assert.equal(await page.locator("#instruction").inputValue(), "让机器人 2 去 (7, 6)");
      await page.getByRole("button", { name: "双机器人示例", exact: true }).click();
      assert.match(await page.locator("#instruction").inputValue(), /机器人 1.*机器人 2/);
      await page.getByRole("button", { name: "规则基线", exact: true }).click();
      assert.match(await page.getByRole("button", { name: "规则基线", exact: true }).getAttribute("class"), /chosen/);
      await page.getByRole("button", { name: "模型 AI", exact: true }).click();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow, false, `${viewport.width}px horizontal overflow`);
      await page.getByRole("button", { name: "新建指令", exact: true }).click();
      await page.evaluate(() => scrollTo(0, 0));
      const box = await page.getByRole("button", { name: "解析并执行任务", exact: true }).boundingBox();
      assert.ok(box && box.y >= 0 && box.y + box.height <= viewport.height, "primary action must fit first viewport");
      await page.screenshot({ path: `${output}home-${viewport.width}.png`, scale: "css" });
      console.log(`PASS viewport ${viewport.width}: controls, empty input, primary action, no overflow`);
    } finally {
      await context.close();
    }
  }
  assert.deepEqual(errors, [], "browser runtime errors");
}

if (typeof process !== "undefined" && process.argv[1] === fileURLToPath(import.meta.url)) {
  const browser = await chromium.launch({ headless: true });
  try { await smoke(browser); } finally { await browser.close(); }
}
