"use strict";

const reviewForm = document.querySelector("#codebook-review");
const acceptAllButton = document.querySelector("#accept-all-proposals");
if (reviewForm && acceptAllButton) {
  acceptAllButton.addEventListener("click", () => {
    const decisions = reviewForm.querySelectorAll('select[name^="action_"]');
    for (const decision of decisions) {
      decision.value = "accept";
      decision.dispatchEvent(new Event("change", { bubbles: true }));
    }
    for (const target of reviewForm.querySelectorAll('select[name^="target_"]')) {
      target.value = "";
    }
    document.querySelector("#review-batch-status").textContent =
      `All ${decisions.length} proposals selected for acceptance. You can adjust individual decisions before clicking Apply review decisions.`;
  });
}
