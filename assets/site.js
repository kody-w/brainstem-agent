const installPanel = document.querySelector(".install-panel");
const platforms = document.querySelectorAll('input[name="platform"]');
const commands = document.querySelectorAll(".command-panel");
const copyStatus = document.querySelector("#copy-status");

function selectPlatform(platform) {
  for (const command of commands) {
    command.hidden = command.dataset.platform !== platform;
  }
  copyStatus.textContent = "";
}

installPanel.classList.add("is-enhanced");
for (const platform of platforms) {
  platform.addEventListener("change", () => {
    if (platform.checked) selectPlatform(platform.value);
  });
}
selectPlatform(document.querySelector('input[name="platform"]:checked').value);

for (const button of document.querySelectorAll("[data-copy]")) {
  button.hidden = false;
  button.addEventListener("click", async () => {
    const command = document.getElementById(button.dataset.copy);
    copyStatus.textContent = "";
    try {
      await navigator.clipboard.writeText(command.textContent);
      copyStatus.textContent = "Command copied.";
    } catch {
      copyStatus.textContent = "Could not copy. Select the command and copy it manually.";
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(command);
      selection.removeAllRanges();
      selection.addRange(range);
      command.closest("pre").focus();
    }
  });
}
