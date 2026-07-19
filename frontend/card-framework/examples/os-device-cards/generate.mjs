// SPDX-License-Identifier: GPL-3.0-or-later
// 从 schemas.json(DeviceSchema[])生成每个 OS 设备的自包含特色卡 HTML + 一张 gallery 总览。
// 卡片 = 任意代码(此处为自包含 HTML 片段),数据由确定性 mock 提供,证明"schema → 特色前端卡"。
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const schemas = JSON.parse(readFileSync(join(here, 'out', 'schemas.json'), 'utf8'))

// —— 确定性 mock:按字段名 hash 生成稳定值,避免 Date.now/random ——
function hash(s) {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) }
  return (h >>> 0)
}
function mockNumber(name) { return Math.round((hash(name) % 1000) / 10) } // 0..99.9
function mockString(name) {
  const opts = ['idle', 'running', 'ready', 'ok', 'busy']
  return opts[hash(name) % opts.length]
}
const STATUS_COLOR = { running: '#34d399', busy: '#fbbf24', idle: '#94a3b8', ready: '#60a5fa', ok: '#34d399', offline: '#f87171' }

function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])) }

// —— 单字段渲染:数值→仪表读数,字符串→状态徽章,bool→指示灯 ——
function renderField(f) {
  if (f.type === 'number') {
    const v = mockNumber(f.name)
    const unit = f.unit ? `<span class="unit">${esc(f.unit)}</span>` : ''
    const pct = Math.min(100, v)
    return `<div class="metric">
      <div class="metric-label">${esc(f.name)}</div>
      <div class="metric-value">${v.toFixed(1)}${unit}</div>
      <div class="bar"><span style="width:${pct}%"></span></div>
    </div>`
  }
  if (f.type === 'bool') {
    const on = hash(f.name) % 2 === 0
    return `<div class="metric">
      <div class="metric-label">${esc(f.name)}</div>
      <div class="dot ${on ? 'on' : 'off'}"></div>
    </div>`
  }
  const v = mockString(f.name)
  const col = STATUS_COLOR[v] || '#94a3b8'
  return `<div class="metric">
    <div class="metric-label">${esc(f.name)}</div>
    <div class="badge" style="--c:${col}">${esc(v)}</div>
  </div>`
}

// —— 动作渲染:参数少的展开 input,否则只出按钮 ——
function renderAction(a) {
  const params = a.params && typeof a.params === 'object' ? Object.keys(a.params) : []
  const shown = params.slice(0, 3)
  const inputs = shown.map((p) => {
    const t = (a.params[p] && a.params[p].type) || 'string'
    const kind = (t === 'number' || t === 'integer') ? 'number' : 'text'
    return `<label class="param"><span>${esc(p)}</span><input type="${kind}" placeholder="${esc(t)}"/></label>`
  }).join('')
  const more = params.length > shown.length ? `<span class="more">+${params.length - shown.length}</span>` : ''
  return `<div class="action">
    <button onclick="window.__toast&&window.__toast('${esc(a.name)}')">${esc(a.name)}</button>
    ${inputs}${more}
  </div>`
}

const CSS = `
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0b1120;color:#e2e8f0;font:14px/1.5 -apple-system,'Segoe UI',system-ui,sans-serif}
.card{background:linear-gradient(180deg,#111a2e,#0e1626);border:1px solid #1e2a44;border-radius:16px;padding:18px 18px 16px;width:360px;box-shadow:0 8px 30px rgba(0,0,0,.35)}
.card h3{margin:0;font-size:15px;font-weight:650;letter-spacing:.2px;color:#f1f5f9;font-family:'Cascadia Mono',ui-monospace,monospace;word-break:break-all}
.chip{display:inline-block;margin-top:6px;padding:2px 9px;border-radius:999px;background:#16233c;color:#7dd3fc;font-size:11px;border:1px solid #24344f}
.desc{margin:8px 0 14px;color:#94a3b8;font-size:12px;max-height:34px;overflow:hidden}
.section-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin:12px 0 8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.metric{background:#0c1425;border:1px solid #1c2a45;border-radius:10px;padding:8px 10px;min-width:0}
.metric-label{font-size:11px;color:#8aa0c0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric-value{font-size:18px;font-weight:600;font-variant-numeric:tabular-nums;color:#f8fafc;font-family:ui-monospace,monospace}
.unit{font-size:11px;color:#7c8db0;margin-left:3px}
.bar{height:4px;background:#1c2a45;border-radius:3px;margin-top:6px;overflow:hidden}
.bar span{display:block;height:100%;background:linear-gradient(90deg,#38bdf8,#818cf8)}
.badge{display:inline-block;padding:2px 10px;border-radius:6px;font-size:12px;font-weight:600;color:var(--c);background:color-mix(in srgb,var(--c) 16%,transparent);border:1px solid color-mix(in srgb,var(--c) 35%,transparent)}
.dot{width:12px;height:12px;border-radius:50%;margin-top:4px}
.dot.on{background:#34d399;box-shadow:0 0 8px #34d39988}.dot.off{background:#475569}
.actions{display:flex;flex-direction:column;gap:6px;margin-top:4px}
.action{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.action button{background:#1d4ed8;color:#fff;border:0;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:ui-monospace,monospace}
.action button:hover{background:#2563eb}
.param{display:flex;align-items:center;gap:4px;font-size:11px;color:#8aa0c0}
.param input{width:64px;background:#0c1425;border:1px solid #24344f;border-radius:6px;color:#e2e8f0;padding:3px 6px;font-size:11px}
.more{font-size:11px;color:#64748b}
.empty{color:#64748b;font-size:12px;font-style:italic}
`

function cardFragment(s) {
  const fields = s.fields.length
    ? `<div class="section-title">遥测 / 状态 · ${s.fields.length}</div><div class="grid">${s.fields.map(renderField).join('')}</div>`
    : ''
  const actions = s.actions.length
    ? `<div class="section-title">控制 · ${s.actions.length}</div><div class="actions">${s.actions.slice(0, 6).map(renderAction).join('')}${s.actions.length > 6 ? `<span class="more">…还有 ${s.actions.length - 6} 个动作</span>` : ''}</div>`
    : '<div class="empty">无动作</div>'
  return `<div class="card" data-device="${esc(s.deviceId)}">
    <h3>${esc(s.deviceId)}</h3>
    ${s.category ? `<span class="chip">${esc(s.category)}</span>` : ''}
    ${s.description ? `<div class="desc">${esc(s.description)}</div>` : ''}
    ${fields}
    ${actions}
  </div>`
}

const TOAST = `<script>window.__toast=function(a){var t=document.createElement('div');t.textContent='▶ '+a+'(...)';t.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#1d4ed8;color:#fff;padding:8px 16px;border-radius:8px;font:13px monospace;z-index:99';document.body.appendChild(t);setTimeout(function(){t.remove()},1500)}<\/script>`

function standaloneDoc(s) {
  return `<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>${esc(s.deviceId)}</title><style>${CSS} body{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}</style></head><body>${cardFragment(s)}${TOAST}</body></html>`
}

// —— 输出 ——
const outDir = join(here, 'out')
let n = 0
for (const s of schemas) {
  const safe = s.deviceId.replace(/[^a-zA-Z0-9_.-]/g, '_')
  writeFileSync(join(outDir, `${safe}.html`), standaloneDoc(s))
  n++
}
// gallery
const gallery = `<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>OS 设备卡 · Gallery</title><style>${CSS}
body{padding:28px}
h1{font-size:20px;margin:0 0 4px;font-family:ui-monospace,monospace}
.sub{color:#64748b;margin:0 0 22px;font-size:13px}
.gal{display:grid;grid-template-columns:repeat(auto-fill,360px);gap:18px;justify-content:center}
</style></head><body>
<h1>Uni-Lab-OS 设备特色卡 · card-framework</h1>
<p class="sub">${n} 个设备类 · schema 由 registry YAML 离线抽取(与 OsAdapter 映射一致)· 卡片自动生成 · mock 数据</p>
<div class="gal">${schemas.map(cardFragment).join('')}</div>${TOAST}</body></html>`
writeFileSync(join(outDir, 'index.html'), gallery)
console.log(`生成 ${n} 张设备卡 + gallery → ${outDir}/index.html`)
