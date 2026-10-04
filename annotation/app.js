"use strict";
const data = JSON.parse(document.getElementById("data").textContent);
const $ = id => document.getElementById(id);
const key = "manuscript-annotation-v1:" + data.source;
let comments = [], editing = null, selected = null, storageBroken = false;
const targets = new Map();
function notice(message, error = false) { $("notice").textContent = message; $("notice").classList.toggle("error", error); }
try {
  const stored = JSON.parse(localStorage.getItem(key) || "[]");
  if (!Array.isArray(stored) || stored.some(c => !c || ["id", "paragraph_id", "type", "priority", "instruction", "reason", "status"].some(k => typeof c[k] !== "string"))) throw new Error("Invalid saved comments");
  comments = stored;
} catch (error) {
  storageBroken = true;
  notice("Saved comments could not be read. Existing storage will not be overwritten; new comments are temporary. Export before closing.", true);
}
function save() {
  if (storageBroken) { notice("Browser storage is unavailable. Export your comments before closing.", true); return; }
  try { localStorage.setItem(key, JSON.stringify(comments)); notice("Saved in this browser. Export a copy to keep your comments."); }
  catch (error) { notice("Browser storage failed. Comments remain in this tab; export before closing.", true); }
}
function element(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; }
function openEditor(block, comment = null) {
  selected = block ? block.id : comment.paragraph_id;
  editing = comment ? comment.id : null;
  $("editor").hidden = false;
  $("editor-title").textContent = editing ? "Edit comment" : "New comment";
  $("target").textContent = (block ? block.sec || "Manuscript" : "Paragraph no longer in source") + " · " + selected;
  for (const field of ["type", "priority", "instruction", "reason", "status"]) $(field).value = comment ? comment[field] : ({type:"clarity",priority:"medium",status:"open"}[field] || "");
  mark();
  $("instruction").focus();
}
for (const block of data.blocks) {
  if (block.t === "heading") { $("manuscript").append(element("h" + block.lv, block.title || block.text.replace(/^#+\s*/, ""))); continue; }
  if (["meta", "comment"].includes(block.t)) continue;
  const node = element("div", undefined, "block");
  node.id = block.id; node.tabIndex = 0; node.setAttribute("role", "button"); node.setAttribute("aria-label", "Annotate: " + block.text);
  if (block.t === "table") {
    const table = element("table");
    block.text.split("\n").filter(line => !/^\s*\|[\s:|\-]+\|\s*$/.test(line)).forEach((line, index) => {
      const row = element("tr"); line.trim().replace(/^\||\|$/g, "").split("|").forEach(cell => row.append(element(index ? "td" : "th", cell.trim()))); table.append(row);
    });
    node.append(table);
  } else node.append(element(["math", "code"].includes(block.t) ? "pre" : "p", block.text));
  node.append(element("small", block.id), element("span", "", "badge"));
  node.onclick = () => openEditor(block);
  node.onkeydown = event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openEditor(block); } };
  targets.set(block.id, {node, block}); $("manuscript").append(node);
}
function mark() {
  for (const [id, {node}] of targets) {
    const count = comments.filter(c => c.paragraph_id === id).length;
    node.classList.toggle("commented", count > 0); node.classList.toggle("selected", selected === id);
    node.setAttribute("aria-pressed", String(selected === id));
    node.querySelector(".badge").textContent = count ? `${count} comment${count === 1 ? "" : "s"}` : "";
  }
}
function render() {
  $("count").textContent = comments.length;
  $("comments").replaceChildren();
  if (!comments.length) $("comments").append(element("p", "No comments yet. Select a paragraph to add your first revision request.", "empty"));
  for (const comment of comments) {
    const card = element("article", undefined, "card");
    card.append(element("div", `${comment.type} · ${comment.priority} priority · ${comment.status}`, "meta"), element("p", comment.instruction));
    if (comment.reason) card.append(element("p", "Reason: " + comment.reason));
    const target = targets.get(comment.paragraph_id);
    card.append(element("div", (target ? "" : "Target missing — original text retained. ") + comment.paragraph_id, "meta"));
    if (!target && comment.quote) card.append(element("p", comment.quote));
    const actions = element("div", undefined, "actions");
    function action(label, handler) { const button = element("button", label, "secondary"); button.type = "button"; button.onclick = handler; actions.append(button); }
    if (target) action("Go to paragraph", () => { selected = comment.paragraph_id; mark(); target.node.scrollIntoView({behavior:"smooth", block:"center"}); target.node.focus({preventScroll:true}); });
    action("Edit", () => openEditor(target && target.block, comment));
    action("Delete", () => { if (!confirm("Delete this comment?")) return; comments = comments.filter(c => c.id !== comment.id); if (editing === comment.id) closeEditor(); save(); render(); });
    card.append(actions); $("comments").append(card);
  }
  mark();
}
function closeEditor() { editing = null; selected = null; $("editor").hidden = true; mark(); }
$("cancel").onclick = closeEditor;
$("editor").onsubmit = event => {
  event.preventDefault();
  if (!$("instruction").value.trim()) { $("instruction").setCustomValidity("Enter an instruction."); $("instruction").reportValidity(); return; }
  const previous = comments.find(c => c.id === editing);
  const block = targets.get(selected)?.block;
  const comment = previous ? {...previous} : {id: "c-" + crypto.randomUUID(), paragraph_id: selected, source: data.source, section: block.sec, quote: block.text};
  for (const field of ["type", "priority", "instruction", "reason", "status"]) comment[field] = $(field).value.trim();
  if (previous) comments = comments.map(c => c.id === editing ? comment : c); else comments.push(comment);
  save(); closeEditor(); render();
};
$("instruction").oninput = () => $("instruction").setCustomValidity("");
$("export").onclick = () => {
  const text = comments.map(comment => JSON.stringify(comment)).join("\n") + (comments.length ? "\n" : "");
  const url = URL.createObjectURL(new Blob([text], {type:"application/x-ndjson;charset=utf-8"}));
  const link = element("a"); link.href = url; link.download = "comments.jsonl"; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
render();
