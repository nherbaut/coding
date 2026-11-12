"use strict";

for (const form of document.querySelectorAll("form[data-removal-confirm]")) {
  form.addEventListener("submit", event => {
    if (!window.confirm(form.dataset.removalConfirm)) event.preventDefault();
  });
}

for (const form of document.querySelectorAll("form[data-private-link]")) {
  const status = form.querySelector('[role="status"]');
  const input = form.querySelector("[data-private-link-url]");
  const copy = form.querySelector("[data-copy-private-link]");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    input.value = "";
    input.parentElement.hidden = true;
    copy.hidden = true;
    status.textContent = "Loading…";
    try {
      const response = await fetch(form.getAttribute("action") || window.location.href, {
        method: "POST", body: new FormData(form), credentials: "same-origin",
        headers: {"Accept": "application/json"}, cache: "no-store"
      });
      if (!response.headers.get("Content-Type")?.includes("application/json")) {
        throw new Error("Sign in again to view this private link.");
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Unable to show the private link.");
      input.value = data.url;
      input.parentElement.hidden = false;
      copy.hidden = false;
      status.textContent = "Keep this link private. Viewing it does not replace it.";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = "Private link copied.";
    } catch {
      input.focus();
      input.select();
      status.textContent = "Select and copy the private link above.";
    }
  });
}
