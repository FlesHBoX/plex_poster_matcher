"""
Generates output/overrides.html from output/multi_options.json — a
self-contained, double-click-to-open page (no server needed) for
reviewing every title where more than one target-uploader poster was
found, and picking a different one than the script auto-selected.

Usage:
    python generate_overrides_page.py

Run this after main.py. Re-run it any time multi_options.json changes
(e.g. after a --reverify pass) to refresh the page with new entries —
your in-progress selections are kept (they're stored in the browser via
localStorage, keyed by title/year/type, not in the HTML file itself).

Workflow:
    1. Run main.py (or main.py --dry-run) as usual.
    2. Run this script.
    3. Open output/overrides.html in a browser.
    4. Click a title, click whichever poster you'd rather use.
    5. Click "Export overrides.csv" — downloads a manual.csv-formatted
       file containing just the titles you actually changed.
    6. Merge that into output/manual.csv (open both in a text editor and
       copy the rows over, or use merge_overrides.py to do it for you).
    7. Run main.py again — your override applies automatically, same as
       any other manual.csv entry.
"""
import json
import os
import sys

import config

JSON_PATH = os.path.join(config.OUTPUT_DIR, "multi_options.json")
HTML_PATH = os.path.join(config.OUTPUT_DIR, "overrides.html")

HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Poster Overrides</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, -apple-system, sans-serif; background:#111; color:#eee; margin:0; padding:24px; }
  h1 { font-weight:600; margin:0 0 4px; }
  .subtitle { color:#999; margin:0 0 20px; font-size:14px; }
  .toolbar { display:flex; gap:14px; align-items:center; margin-bottom:22px; flex-wrap:wrap; }
  button { background:#2e8b57; color:#fff; border:none; padding:10px 18px; border-radius:6px;
           cursor:pointer; font-size:14px; font-weight:500; }
  button:hover { background:#37a366; }
  button.secondary { background:#333; }
  button.secondary:hover { background:#444; }
  #summary { color:#aaa; font-size:14px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(170px,1fr)); gap:18px; }
  .card { background:#1c1c1c; border-radius:10px; overflow:hidden; cursor:pointer;
          border:2px solid transparent; transition:border-color .15s; }
  .card:hover { border-color:#555; }
  .card.decided { border-color:#2e8b57; }
  .card img { width:100%; display:block; aspect-ratio:2/3; object-fit:cover; background:#000; }
  .card .label { padding:10px; font-size:13px; line-height:1.4; }
  .card .count { color:#999; font-size:12px; margin-top:2px; }
  .modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.85); display:none;
                     align-items:flex-start; justify-content:center; z-index:10; overflow-y:auto; padding:40px 20px; }
  .modal-backdrop.open { display:flex; }
  .modal { background:#1c1c1c; border-radius:12px; padding:28px; max-width:960px; width:100%; }
  .modal h2 { margin-top:0; }
  .options { display:flex; gap:18px; flex-wrap:wrap; margin-top:18px; }
  .option { cursor:pointer; border:3px solid transparent; border-radius:8px; overflow:hidden;
            width:170px; transition:border-color .15s; }
  .option:hover { border-color:#555; }
  .option.chosen { border-color:#2e8b57; }
  .option.selected { border-color:#e8a33d; }
  .option img { width:100%; display:block; aspect-ratio:2/3; object-fit:cover; background:#000; }
  .option .uploader { padding:8px; text-align:center; font-size:13px; background:#252525; }
  .badge { display:inline-block; font-size:10px; padding:2px 7px; border-radius:10px; margin-top:4px; }
  .badge.default { background:#2e8b57; }
  .badge.override { background:#e8a33d; color:#000; }
  .close-btn { float:right; }
  .empty { color:#999; padding:40px 0; text-align:center; }
</style>
</head>
<body>
  <h1>Poster Overrides</h1>
  <p class="subtitle">Titles where more than one target-uploader poster was found. Click a title to review options.</p>
  <div class="toolbar">
    <button id="exportBtn">Export overrides.csv</button>
    <button id="resetBtn" class="secondary">Reset all selections</button>
    <span id="summary"></span>
  </div>
  <div class="grid" id="grid"></div>

  <div class="modal-backdrop" id="modalBackdrop">
    <div class="modal" id="modal"></div>
  </div>

<script>
const DATA = __DATA__;
const STORAGE_KEY = "posterOverrides_v1";

function loadSelections() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; }
  catch (e) { return {}; }
}
function saveSelections(sel) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sel));
}
let selections = loadSelections();  // key -> chosen poster_id (only present when overridden)

function keyFor(item) { return item.title + "|" + item.year + "|" + item.type; }

function render() {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  if (DATA.length === 0) {
    grid.innerHTML = '<div class="empty">No multi-option titles yet — run main.py first.</div>';
    document.getElementById("summary").textContent = "";
    return;
  }

  let decidedCount = 0;
  DATA.forEach(item => {
    const k = keyFor(item);
    const overrideId = selections[k];
    const isDecided = overrideId !== undefined;
    if (isDecided) decidedCount++;
    const shownOpt = overrideId
      ? item.options.find(o => o.poster_id === overrideId)
      : (item.options.find(o => o.image_url === item.chosen_image_url) || item.options[0]);

    const card = document.createElement("div");
    card.className = "card" + (isDecided ? " decided" : "");
    card.innerHTML =
      '<img src="' + shownOpt.image_url + '" loading="lazy">' +
      '<div class="label">' + item.title + ' (' + item.year + ')' +
      '<div class="count">' + item.options.length + ' options' + (overrideId ? " &middot; overridden" : "") + '</div></div>';
    card.onclick = () => openModal(item);
    grid.appendChild(card);
  });

  document.getElementById("summary").textContent =
    DATA.length + " title(s) with multiple options \u2014 " + decidedCount + " overridden";
}

function openModal(item) {
  const k = keyFor(item);
  const overrideId = selections[k];
  const modal = document.getElementById("modal");

  const optionsHtml = item.options.map(o => {
    const isDefault = o.image_url === item.chosen_image_url;
    const isSelected = overrideId ? (overrideId === o.poster_id) : isDefault;
    let badges = "";
    if (isDefault) badges += '<span class="badge default">default</span>';
    if (!isDefault && isSelected) badges += '<span class="badge override">your pick</span>';
    return '<div class="option ' + (isDefault ? "chosen" : "") + ' ' + (isSelected ? "selected" : "") +
           '" data-poster-id="' + o.poster_id + '">' +
           '<img src="' + o.image_url + '" loading="lazy">' +
           '<div class="uploader">' + o.uploader + '<br>' + badges + '</div></div>';
  }).join("");

  modal.innerHTML =
    '<button class="close-btn secondary" id="closeBtn">Close</button>' +
    '<h2>' + item.title + ' (' + item.year + ')</h2>' +
    '<div class="options">' + optionsHtml + '</div>';

  modal.querySelector("#closeBtn").onclick = closeModal;
  modal.querySelectorAll(".option").forEach(el => {
    el.onclick = () => {
      const pid = el.getAttribute("data-poster-id");
      const chosen = item.options.find(o => o.poster_id === pid);
      if (chosen.image_url === item.chosen_image_url) {
        delete selections[k];  // picking the default again just clears any override
      } else {
        selections[k] = pid;
      }
      saveSelections(selections);
      render();
      openModal(item);  // refresh highlighting in place
    };
  });

  document.getElementById("modalBackdrop").classList.add("open");
}
function closeModal() {
  document.getElementById("modalBackdrop").classList.remove("open");
}
document.getElementById("modalBackdrop").addEventListener("click", (e) => {
  if (e.target.id === "modalBackdrop") closeModal();
});

document.getElementById("resetBtn").onclick = () => {
  if (!confirm("Clear all your override selections? This can't be undone.")) return;
  selections = {};
  saveSelections(selections);
  render();
};

document.getElementById("exportBtn").onclick = () => {
  const rows = [["title", "year", "type", "source_url", "local_file", "note", "applied"]];
  let count = 0;
  DATA.forEach(item => {
    const k = keyFor(item);
    const overrideId = selections[k];
    if (!overrideId) return;
    const chosen = item.options.find(o => o.poster_id === overrideId);
    if (!chosen || chosen.image_url === item.chosen_image_url) return;
    count++;
    const posterUrl = "https://theposterdb.com/poster/" + chosen.poster_id;
    rows.push([item.title, item.year, item.type, posterUrl, "", "override: " + chosen.uploader, ""]);
  });
  if (count === 0) {
    alert("No overrides selected yet \u2014 click a title, then click a different poster to override it.");
    return;
  }
  const csv = rows.map(r => r.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(",")).join("\\r\\n");
  const blob = new Blob([csv], {type: "text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "overrides.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
};

render();
</script>
</body>
</html>
"""


def main():
    if not os.path.exists(JSON_PATH):
        sys.exit(
            f"{JSON_PATH} doesn't exist yet.\n"
            "It's created automatically by main.py whenever a title has more than "
            "one target-uploader poster — run main.py first."
        )

    with open(JSON_PATH, encoding="utf-8") as f:
        data_text = f.read()
    json.loads(data_text)  # validate it's parseable before embedding

    html = HTML_TEMPLATE.replace("__DATA__", data_text)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    count = len(json.loads(data_text))
    print(f"Wrote {HTML_PATH} ({count} title(s) with multiple options).")
    print("Open it in a browser to review and override poster choices.")


if __name__ == "__main__":
    main()
