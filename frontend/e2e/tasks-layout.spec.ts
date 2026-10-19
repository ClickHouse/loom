import { expect, test } from "@playwright/test";
import { installApi } from "./fixtures";

// A task row beside the open detail panel, at the widths where the two share the page:
// the title keeps its room and no two parts of the row overlap.

for (const width of [1280, 1400, 1440]) {
  test(`at ${width} px a task row beside the open panel keeps its title and no two parts overlap`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    await page.goto("/tasks");
    await page.getByRole("row", { name: /^Task 1/ }).click();
    await expect(page.getByRole("complementary", { name: "Task detail" })).toBeVisible();
    const problems = await page.evaluate(() => {
      const out: string[] = [];
      const box = (e: Element) => e.getBoundingClientRect();
      const hits = (a: DOMRect, b: DOMRect) =>
        a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
      for (const row of document.querySelectorAll(".tq-row")) {
        const title = row.querySelector(".title");
        if (!title || box(title).width < 40)
          out.push(`${row.getAttribute("aria-label")}: title ${box(title!).width}px`);
        // The priority, the title, each piece of the meta (Accept, assignee, age, menu) and the
        // second line (the kind, the state's line).
        const parts = [...row.querySelectorAll(".tq-pri, .title, .tq-meta > *, .tq-sub > *")].filter(
          (e) => box(e).width > 0,
        );
        for (let i = 0; i < parts.length; i++)
          for (let j = i + 1; j < parts.length; j++)
            if (!parts[i].contains(parts[j]) && hits(box(parts[i]), box(parts[j])))
              out.push(`${row.getAttribute("aria-label")}: ${parts[i].className} over ${parts[j].className}`);
      }
      // A group's name stays on one line.
      for (const name of document.querySelectorAll("[id^='tq-group-']"))
        if (box(name).height > 24) out.push(`group ${name.textContent} wraps`);
      return out;
    });
    expect(problems).toEqual([]);
  });
}
