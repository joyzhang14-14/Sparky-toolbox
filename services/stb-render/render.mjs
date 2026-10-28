// stb-render 容器入口：stdin 一行 JSON 任务，stdout 最后一行 JSON 结果。只认 file: / data: / blob: 请求（容器本身也断网）。
// {mode: "png", html, out, width, height, scale, transparent, full_page}
// {mode: "pdf", html, out, width, height, previews: [页码…], preview_dir}   预览用 pdftoppm 转 PNG（长边 1024）
// {mode: "office", file, out, previews, preview_dir}                       LibreOffice 把 pptx / docx / xlsx 转成 PDF（out），再出预览
// {mode: "fonts"}                                                           输出 catalog.json
import { copyFileSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { chromium } from "playwright-core";

const job = JSON.parse(readFileSync(0, "utf8"));
// 等管道写完再退出（直接 exit 会把超过 64 KB 的输出截断）；返回永不完成的 Promise，调用处 await 它就不会往下走
const done = (r) =>
  new Promise(() => process.stdout.write(JSON.stringify(r) + "\n", () => process.exit(0)));

if (job.mode === "fonts")
  await done({ fonts: JSON.parse(readFileSync("/opt/render/catalog.json", "utf8")) });

// PDF 选中的页转成 PNG 预览（长边 1024），返回 {pages, previews}
function pdfPreviews(pdf, wanted, dir) {
  const pages = Number((execFileSync("pdfinfo", [pdf]).toString().match(/^Pages:\s+(\d+)/m) || [])[1] || 0);
  const previews = [];
  for (const n of wanted || []) {
    if (n < 1 || n > pages) continue;
    const base = `${dir}/page-${n}`;
    execFileSync("pdftoppm", ["-png", "-f", String(n), "-l", String(n), "-scale-to", "1024", "-singlefile", pdf, base]);
    previews.push({ page: n, file: base + ".png" });
  }
  return { pages, previews };
}

if (job.mode === "office") {
  // 配置目录放 /tmp（根只读）；输出先落 /tmp 再拷到 media（soffice 按源文件名命名输出）
  const name = job.file.split("/").pop().replace(/\.[^.]+$/, "");
  execFileSync("soffice", ["-env:UserInstallation=file:///tmp/lo-profile", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", "/tmp/office", job.file],
               { timeout: 150000, stdio: ["ignore", "ignore", "pipe"] });
  copyFileSync(`/tmp/office/${name}.pdf`, job.out);
  await done({ ...pdfPreviews(job.out, job.previews, job.preview_dir), problems: [] });
}

const browser = await chromium.launch({
  args: [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--font-render-hinting=none",
  ],
});
const page = await (
  await browser.newContext({
    viewport: { width: job.width, height: job.height },
    deviceScaleFactor: job.scale || 1,
  })
).newPage();
const problems = [];
await page.route("**/*", (r) => {
  const u = r.request().url();
  if (/^(file|data|blob):/.test(u)) return r.continue();
  problems.push(
    `拦下了外部请求（容器断网，只能用工作目录里的文件和已装字体）：${u.slice(0, 200)}`,
  );
  return r.abort();
});
page.on("requestfailed", (q) => {
  if (q.url().startsWith("file:"))
    problems.push(`文件没加载到：${decodeURI(q.url().slice(7))}`);
});
page.on("pageerror", (e) =>
  problems.push(`页面脚本出错：${String(e.message).slice(0, 300)}`),
);
page.on("console", (m) => {
  if (m.type() === "error")
    problems.push(`console.error：${m.text().slice(0, 300)}`);
});

await page.goto("file://" + job.html, { waitUntil: "load", timeout: 60000 });
const info = await page.evaluate(async () => {
  await document.fonts.ready;
  const missing = [...document.fonts]
    .filter((f) => f.status === "error")
    .map((f) => f.family);
  const el = document.scrollingElement || document.documentElement;
  const broken = [...document.images]
    .filter((i) => i.complete && i.naturalWidth === 0)
    .map((i) => i.getAttribute("src"));
  // 内容被 overflow: hidden / clip 裁掉的元素（最常见：幻灯片一页字太多）
  const clipped = [...document.body.querySelectorAll("*")]
    .filter((e) => {
      const s = getComputedStyle(e);
      const cut = (v) => v === "hidden" || v === "clip";
      return (cut(s.overflowY) && e.scrollHeight > e.clientHeight + 2) || (cut(s.overflowX) && e.scrollWidth > e.clientWidth + 2);
    })
    .slice(0, 5)
    .map((e) => `<${e.tagName.toLowerCase()}${e.className ? "." + String(e.className).split(" ")[0] : ""}>「${(e.innerText || "").trim().slice(0, 20)}」内容 ${e.scrollWidth}×${e.scrollHeight}，框 ${e.clientWidth}×${e.clientHeight}`);
  return {
    scroll_width: el.scrollWidth,
    scroll_height: el.scrollHeight,
    missing_fonts: missing,
    broken_images: broken,
    clipped,
  };
});
for (const c of info.clipped) problems.push(`内容超出框被裁掉：${c}`);
for (const s of info.broken_images) problems.push(`图片没显示出来：${s}`);
for (const f of info.missing_fonts) problems.push(`字体没加载到：${f}`);

const out = {
  width: job.width,
  height: job.height,
  content_width: info.scroll_width,
  content_height: info.scroll_height,
};
if (job.mode === "png") {
  if (
    !job.full_page &&
    (info.scroll_width > job.width || info.scroll_height > job.height)
  )
    problems.push(
      `内容超出画布（内容 ${info.scroll_width}×${info.scroll_height}，画布 ${job.width}×${job.height}），超出部分被裁掉了`,
    );
  await page.screenshot({
    path: job.out,
    fullPage: !!job.full_page,
    omitBackground: !!job.transparent,
  });
} else {
  await page.pdf({
    path: job.out,
    width: `${job.width}px`,
    height: `${job.height}px`,
    printBackground: true,
    preferCSSPageSize: true,
  });
  Object.assign(out, pdfPreviews(job.out, job.previews, job.preview_dir));
}
await browser.close();
await done({ ...out, problems: [...new Set(problems)].slice(0, 20) });
