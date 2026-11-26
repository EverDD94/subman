const $ = id => document.getElementById(id);
async function paint() {
  const {url = "http://192.168.1.121:8787", code = "", last} = await chrome.storage.local.get(["url", "code", "last"]);
  $("url").value = url;
  $("code").value = code;
  if (last) {
    $("dot").style.background = last.ok ? "#2e9e5b" : "#d33";
    $("msg").textContent = (last.ok ? "Conectado · " : (last.error || "Error") + " · ") + new Date(last.at).toLocaleString();
  }
}
$("go").onclick = async () => {
  await chrome.storage.local.set({url: $("url").value.trim(), code: $("code").value.trim().toUpperCase()});
  $("msg").textContent = "Enviando…";
  await chrome.runtime.sendMessage("push");
  paint();
};
paint();
