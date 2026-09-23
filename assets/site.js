const installPanel = document.querySelector(".install-panel");
const platforms = document.querySelectorAll('input[name="platform"]');
const commands = document.querySelectorAll(".command-panel");
const platformStatus = document.querySelector("#copy-status");

function selectPlatform(platform) {
  for (const command of commands) {
    command.hidden = command.dataset.platform !== platform;
  }
  platformStatus.textContent = "";
}

installPanel.classList.add("is-enhanced");
for (const platform of platforms) {
  platform.addEventListener("change", () => {
    if (platform.checked) selectPlatform(platform.value);
  });
}
selectPlatform(document.querySelector('input[name="platform"]:checked').value);

for (const button of document.querySelectorAll("[data-copy]")) {
  const command = document.getElementById(button.dataset.copy);
  const status = document.getElementById(button.dataset.copyStatus) || platformStatus;
  const several = command.textContent.includes("\n");
  button.hidden = false;
  button.addEventListener("click", async () => {
    status.textContent = "";
    try {
      await navigator.clipboard.writeText(command.textContent);
      status.textContent = several ? "Commands copied." : "Command copied.";
    } catch {
      status.textContent = several
        ? "Could not copy. Select the commands and copy them manually."
        : "Could not copy. Select the command and copy it manually.";
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(command);
      selection.removeAllRanges();
      selection.addRange(range);
      command.closest("pre").focus();
    }
  });
}
