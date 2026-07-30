import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const demoFacts =
  /Exit E2|East corridor|Pallet P1|Pallet A|Worker C|JHA-MAT(?:ERIAL)?|ST-24-071|10:16:20/i;
const mojibake = /\uFFFD|\uCA8C/;

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the exact upload-first SiteTrace empty state", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(
    html,
    /<title>Evidence-backed incident investigations · SiteTrace<\/title>/i,
  );
  assert.match(
    html,
    /<h1>Reconstruct the work\.<br\/>Keep every finding traceable\.<\/h1>/,
  );
  assert.match(html, /Start with the source material/);
  assert.match(html, /Investigation title/);
  assert.match(html, /Approved JHA/);
  assert.match(html, /Camera footage/);
  assert.match(html, /Supporting plans/);
  assert.match(html, /Site metadata/);
  assert.match(html, /Site map/);
  assert.match(html, /Nine bounded phases, one audit trail/);
  assert.match(html, /TwelveLabs Jockey/);
  assert.match(html, /OpenAI Luna/);
  assert.match(html, /Neo4j · Terra/);
  assert.match(html, /AWS Strands/);
  assert.match(html, /Results begin with your evidence\./);
  assert.match(
    html,
    /does not determine organizational root cause from video alone\./,
  );

  assert.doesNotMatch(html, demoFacts);
  assert.doesNotMatch(html, mojibake);
  assert.doesNotMatch(
    html,
    /codex-preview|react-loading-skeleton|Building your site/i,
  );
});

test("keeps the API workflow dynamic while exposing a labeled demo shortcut", async () => {
  const app = await readFile(
    new URL("../app/SiteTraceApp.tsx", import.meta.url),
    "utf8",
  );

  assert.match(app, /const body = new FormData\(\)/);
  assert.match(app, /body\.append\("title", title\.trim\(\)\)/);
  assert.match(app, /body\.append\("jha", jha as File\)/);
  assert.match(app, /body\.append\("supporting_documents", file\)/);
  assert.match(app, /body\.append\("site_metadata", siteMetadata\)/);
  assert.match(app, /body\.append\("site_map", siteMap\)/);
  assert.match(app, /body\.append\("videos", file\)/);

  assert.match(app, /requestJson<CaseRecord>\(apiBase, "\/cases"/);
  assert.match(app, /casePath\(created\.case_id\)\}\/investigate/);
  assert.match(app, /casePath\(caseRecord\.case_id\)\}\/approve/);
  assert.match(app, /\/evidence\/\$\{encodeURIComponent/);
  assert.match(app, /\/reports\/\$\{encodeURIComponent\(record\.case_id\)\}\.pdf/);
  assert.match(app, /<video/);
  assert.match(app, /#t=\$\{activeClip\.start_sec\},\$\{activeClip\.end_sec\}/);
  assert.match(app, /investigation\.planned_steps/);
  assert.match(app, /investigation\.graph_metrics/);
  assert.match(app, /investigation\.sponsor_trace/);
  assert.match(app, /buildDemoCase/);
  assert.match(app, /event\.key\.toLowerCase\(\) !== "n"/);

  assert.doesNotMatch(app, demoFacts);
  assert.doesNotMatch(app, mojibake);
  assert.doesNotMatch(app, /localStorage|sessionStorage/);
  assert.doesNotMatch(app, /className="corridor"|fake illustrated/i);
});

test("retains keyboard, screen-reader, reduced-motion, and mobile safeguards", async () => {
  const [app, css] = await Promise.all([
    readFile(new URL("../app/SiteTraceApp.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(app, /role="alert"/);
  assert.match(app, /role="status" aria-live="polite"/);
  assert.match(app, /aria-busy=/);
  assert.match(app, /aria-current=\{state === "active" \? "step"/);
  assert.match(app, /aria-pressed=/);
  assert.match(app, /aria-label=\{`Evidence clip/);
  assert.match(app, /tabIndex=\{-1\}/);
  assert.match(app, /prefers-reduced-motion: reduce/);
  assert.match(app, /minLength=\{3\}/);

  assert.match(css, /button:focus-visible/);
  assert.match(
    css,
    /\.upload-field:has\(input\[type="file"\]:focus-visible\)/,
  );
  assert.match(css, /\.sr-only/);
  assert.match(css, /@media \(max-width: 820px\)/);
  assert.match(css, /@media \(max-width: 560px\)/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /--green:\s*#73c580/i);
  assert.match(css, /--quiet:\s*#66717f/i);
  assert.doesNotMatch(css, mojibake);
});

test("removes disposable starter assets and metadata", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /title: "Evidence-backed incident investigations"/);
  assert.match(layout, /default: "SiteTrace"/);
  assert.match(layout, /template: "%s · SiteTrace"/);
  assert.doesNotMatch(page, /codex-preview|SkeletonPreview/);
  assert.doesNotMatch(layout, /codex-preview|Starter Project/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.doesNotMatch(`${page}\n${layout}\n${packageJson}`, mojibake);

  await assert.rejects(
    access(new URL("../app/_sites-preview/SkeletonPreview.tsx", import.meta.url)),
  );
});
