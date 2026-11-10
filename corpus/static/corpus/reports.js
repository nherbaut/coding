"use strict";
document
  .querySelector("#print-report")
  ?.addEventListener("click", () => window.print());
const panes = [...document.querySelectorAll(".comparison-pane")];
const sync = document.querySelector("#sync-report-scroll");
let scrolling = false;
for (const pane of panes) {
  const text = pane.querySelector(".comparison-text");
  text?.addEventListener("scroll", () => {
    if (!sync?.checked || scrolling) return;
    scrolling = true;
    const ratio =
      text.scrollTop / Math.max(1, text.scrollHeight - text.clientHeight);
    for (const other of panes) {
      const target = other.querySelector(".comparison-text");
      if (target && target !== text)
        target.scrollTop = ratio * (target.scrollHeight - target.clientHeight);
    }
    requestAnimationFrame(() => {
      scrolling = false;
    });
  });
  const select = (node) => {
    for (const selected of pane.querySelectorAll(".report-selected"))
      selected.classList.remove("report-selected");
    node?.classList.add("report-selected");
    node?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };
  const activate = (mark) => {
    const ids = new Set(mark.dataset.annotations.split(" ")),
      cards = [...pane.querySelectorAll(".comparison-annotation")].filter(
        (card) => ids.has(card.dataset.excerpt),
      );
    select(mark);
    for (const card of cards) card.classList.add("report-selected");
    cards[0]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };
  pane.addEventListener("click", (event) => {
    const jump = event.target.closest(".passage-jump");
    if (jump) {
      const card = jump.closest(".comparison-annotation"),
        mark = [...pane.querySelectorAll("mark[data-annotations]")].find(
          (node) =>
            node.dataset.annotations.split(" ").includes(card.dataset.excerpt),
        );
      select(mark);
      card.classList.add("report-selected");
      return;
    }
    const mark = event.target.closest(
      ".comparison-text mark[data-annotations]",
    );
    if (mark) activate(mark);
  });
  pane.addEventListener("keydown", (event) => {
    const mark = event.target.closest(
      ".comparison-text mark[data-annotations]",
    );
    if (mark && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      activate(mark);
    }
  });
}
