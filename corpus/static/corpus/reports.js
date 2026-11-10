"use strict";

document.querySelector("#print-report")?.addEventListener("click", () => window.print());
const panes = [...document.querySelectorAll(".comparison-pane")];
const sync = document.querySelector("#sync-report-scroll");
let scrolling = false;
for (const pane of panes) {
  const text = pane.querySelector(".comparison-text");
  text?.addEventListener("scroll", () => {
    if (!sync?.checked || scrolling) return;
    scrolling = true;
    const ratio = text.scrollTop / Math.max(1, text.scrollHeight - text.clientHeight);
    for (const other of panes) {
      const target = other.querySelector(".comparison-text");
      if (target && target !== text) target.scrollTop = ratio * (target.scrollHeight - target.clientHeight);
    }
    requestAnimationFrame(() => { scrolling = false; });
  });
  const select = (node) => {
    for (const selected of pane.querySelectorAll(".report-selected")) selected.classList.remove("report-selected");
    node?.classList.add("report-selected");
    node?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };
  for (const card of pane.querySelectorAll(".comparison-annotation")) {
    card.querySelector(".passage-jump")?.addEventListener("click", () => {
      const mark = [...pane.querySelectorAll("mark[data-annotations]")].find(node => node.dataset.annotations.split(" ").includes(card.dataset.excerpt));
      select(mark);
      card.classList.add("report-selected");
    });
  }
  for (const mark of pane.querySelectorAll("mark[data-annotations]")) {
    const activate = () => {
      const ids = new Set(mark.dataset.annotations.split(" "));
      const cards = [...pane.querySelectorAll(".comparison-annotation")].filter(card => ids.has(card.dataset.excerpt));
      select(mark);
      for (const card of cards) card.classList.add("report-selected");
      cards[0]?.scrollIntoView({ block: "nearest", inline: "nearest" });
    };
    mark.addEventListener("click", activate);
    mark.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
    });
  }
}
