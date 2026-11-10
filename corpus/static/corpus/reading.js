"use strict";
(() => {
  const data = document.querySelector("#reading-documents");
  if (!data) return;
  for (const item of JSON.parse(data.textContent)) {
    const containers = [
      ...document.querySelectorAll(".reading-document"),
    ].filter((node) => node.dataset.readingKey === item.key);
    const quotes = [
      ...document.querySelectorAll("[data-reading-document]"),
    ].filter((node) => node.dataset.readingDocument === item.key);
    if (!containers.length && !quotes.length) continue;
    const renderInto = (root, formatted) => {
      CorpusMarkdown.render(
        root,
        item.text,
        item.annotations,
        (text, active) => {
          const mark = document.createElement("mark");
          mark.append(text);
          const codes = [...new Set(active.flatMap((a) => a.codes))];
          const colors = [
            ...new Set(
              codes
                .map((code) => item.colors[code])
                .filter((color) => /^#[0-9a-f]{6}$/i.test(color)),
            ),
          ];
          if (colors.length) {
            const stops = colors.flatMap((color, index) => {
              const tint = `color-mix(in srgb, ${color} 40%, white)`;
              return [
                `${tint} ${(index * 100) / colors.length}%`,
                `${tint} ${((index + 1) * 100) / colors.length}%`,
              ];
            });
            mark.style.background = `linear-gradient(to bottom,${stops.join(",")})`;
          }
          mark.title = codes.map((code) => item.labels[code]).join(", ");
          mark.dataset.annotations = active.map((a) => a.id).join(" ");
          mark.dataset.start = Math.min(...active.map((a) => a.start));
          mark.tabIndex = 0;
          mark.setAttribute("role", "button");
          return mark;
        },
        formatted,
      );
      for (const quote of quotes)
        quote.textContent = CorpusMarkdown.visibleText(
          root,
          Number(quote.dataset.readingStart),
          Number(quote.dataset.readingEnd),
        );
      root.dispatchEvent(
        new CustomEvent("reading-rendered", { bubbles: true }),
      );
    };
    if (!containers.length) {
      renderInto(
        document.createElement("div"),
        CorpusMarkdown.isMarkdown(item.name, item.metadata, item.text),
      );
      continue;
    }
    for (const container of containers) {
      const root = container.querySelector(".reading-text"),
        control = container.querySelector(".reading-view");
      const formatted = CorpusMarkdown.isMarkdown(
        item.name,
        item.metadata,
        item.text,
      );
      if (control) {
        control.value = formatted ? "rendered" : "source";
        control.addEventListener("change", () =>
          renderInto(root, control.value === "rendered"),
        );
      }
      renderInto(root, formatted);
    }
  }
})();
