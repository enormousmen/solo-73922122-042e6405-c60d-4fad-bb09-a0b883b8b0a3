
const BLOCK_COUNT = 32;
let selected = new Set();
let lastState = null;

async function call(url, body) {
  const opts = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const resp = await fetch(url, opts);
  const json = await resp.json();
  if (!resp.ok) throw new Error(json.detail || resp.statusText);
  return json;
}
function showErr(e) { document.getElementById("ioErr").textContent = e.message; setTimeout(() => document.getElementById("ioErr").textContent = "", 4000); }

function render(state) {
  lastState = state;
  const flushActive = ["running", "paused"].includes(state.flush.status);
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  state.blocks.forEach(b => {
    const div = document.createElement("div");
    div.className = `block ${b.location}${b.dirty ? " dirty" : ""}${selected.has(b.block) ? " selected" : ""}`;
    if (flushActive && state.flush.results && state.flush.results[b.block]) {
      div.classList.add("flushing");
    }
    div.innerHTML = `<div class="idx">${b.block}</div><div class="meta">${b.location}${b.cache_seq ? " #"+b.cache_seq : (b.platter_seq ? " #"+b.platter_seq : "")}</div>`;
    div.onclick = () => {
      if (flushActive) return;
      selected.has(b.block) ? selected.delete(b.block) : selected.add(b.block);
      render(state);
    };
    grid.appendChild(div);
  });
  document.getElementById("selLine").textContent =
    selected.size ? `待刷块（${selected.size}）: ${[...selected].sort((a,b)=>a-b).join(", ")}` : "未选择块（点击上方块格选择）";

  const f = state.flush;
  const badge = document.getElementById("flushBadge");
  badge.textContent = f.status; badge.className = "badge " + f.status;
  document.getElementById("flushSeq").textContent = f.seq ? `FLUSH #${f.seq}` : "";
  const total = f.total || 0, cursor = f.cursor || 0;
  document.getElementById("bar").style.width = total ? Math.round(cursor / total * 100) + "%" : "0%";
  document.getElementById("progressText").textContent =
    total ? `进度 ${cursor}/${total}（每块约 0.6s）` : "未开始";
  document.getElementById("btnFlush").disabled = flushActive || selected.size === 0;
  document.getElementById("btnPause").disabled = f.status !== "running";
  document.getElementById("btnResume").disabled = f.status !== "paused";

  const ev = document.getElementById("events");
  ev.innerHTML = state.events.slice().reverse().map(e =>
    `<div class="ev kind-${e.kind.replace(/\./g, "-")}"><b>[${e.kind}]</b> ${e.detail}</div>`).join("");
}

document.getElementById("btnDirty").onclick = () => {
  if (!lastState) return;
  selected = new Set(lastState.blocks.filter(b => b.dirty).map(b => b.block));
  render(lastState);
};
document.getElementById("btnClearSel").onclick = () => { selected.clear(); render(lastState); };

document.getElementById("btnWrite").onclick = async () => {
  try {
    const body = {
      block: parseInt(document.getElementById("blk").value, 10),
      data: document.getElementById("wdata").value,
      pad: document.getElementById("pad").checked,
      fua: document.getElementById("fua").checked,
    };
    const json = await call("/api/write", body);
    render(json.state);
  } catch (e) { showErr(e); }
};
document.getElementById("btnRead").onclick = async () => {
  try {
    const block = parseInt(document.getElementById("blk").value, 10);
    const json = await call(`/api/read?block=${block}`);
    const r = json.result;
    const el = document.getElementById("readout");
    el.style.display = "block";
    el.innerHTML = `块 ${r.block}｜来源: <b>${r.source}</b>｜版本 seq: ${r.seq ?? "-"}<br>预览: ${r.preview}<br><span class="muted">hex 前 64 字节: ${r.data_hex.slice(0, 128)}${r.data_hex.length > 128 ? "…" : ""}</span>`;
  } catch (e) { showErr(e); }
};
document.getElementById("btnFlush").onclick = async () => {
  try {
    const json = await call("/api/flush", {selected: [...selected]});
    render(json.state);
  } catch (e) { showErr(e); }
};
document.getElementById("btnPause").onclick = async () => {
  try { render((await call("/api/flush/pause", {})).state); } catch (e) { showErr(e); }
};
document.getElementById("btnResume").onclick = async () => {
  try { render((await call("/api/flush/resume", {})).state); } catch (e) { showErr(e); }
};
document.getElementById("btnPower").onclick = async () => {
  if (!confirm("确认模拟掉电？缓存将被清空，未完成的 FLUSH 作废。")) return;
  try { selected.clear(); render((await call("/api/power-loss", {})).state); } catch (e) { showErr(e); }
};
document.getElementById("btnReset").onclick = async () => {
  if (!confirm("确认复位？将重建镜像并清空全部状态。")) return;
  try { selected.clear(); render((await call("/api/reset", {})).state); } catch (e) { showErr(e); }
};

setInterval(async () => {
  try { render(await (await fetch("/api/state")).json()); } catch (e) {}
}, 700);
call("/api/state").then(render).catch(showErr);
