"use strict";

for (const form of document.querySelectorAll("form[data-removal-confirm]")) {
  form.addEventListener("submit", event => {
    if (!window.confirm(form.dataset.removalConfirm)) event.preventDefault();
  });
}
