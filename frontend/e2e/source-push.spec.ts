import { expect, test } from "@playwright/test";
import { CODE_REPO, codeReposFixture, installApi, knowledgeSourcesFixture } from "./fixtures";
import { violations } from "./axe";

for (const width of [1400, 375]) {
  for (const kind of ["notion", "linear"]) {
    test(`${kind} push setup and secrets remain accessible at ${width}px`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const repos = codeReposFixture().map((repo) => ({
        ...repo,
        my_status: "member" as const,
        my_role: "admin" as const,
        docs_namespace: "code-clickhouse",
      }));
      const sources = knowledgeSourcesFixture().map((source) => ({ ...source, kind }));
      await installApi(page, { codeRepos: repos, knowledgeSources: sources });
      let phase = "disabled";
      const token = "verification-token-".repeat(12);
      const callback = `https://loom.example/hooks/knowledge/code-clickhouse/${"1".repeat(36)}/${"2".repeat(36)}?org=org`;
      const status = (reveal = false) => ({
        enabled: true,
        status: phase,
        provider: kind,
        generation: "generation",
        callback_url: phase === "disabled" ? "" : callback,
        challenge_fingerprint: phase === "pending" && kind === "notion" ? "fingerprint" : "",
        verification_token: reveal && phase === "pending" ? token : "",
        channels: 0,
        expires_at: 0,
      });
      await page.route(/\/v1\/knowledge\.source\.push_/, async (route) => {
        const body = route.request().postDataJSON();
        const op = new URL(route.request().url()).pathname;
        if (op.endsWith("push_configure")) phase = body.enabled ? "pending" : "disabled";
        if (op.endsWith("push_confirm")) {
          expect(body.generation).toBe("generation");
          if (kind === "linear") expect(body.signing_secret).toBe("linear-signing-secret");
          else expect(body.challenge_fingerprint).toBe("fingerprint");
          phase = "active";
        }
        await route.fulfill({ json: status(body.reveal_challenge) });
      });
      await page.goto("/repositories");
      const repo = page.getByRole("region", { name: CODE_REPO, exact: true });
      await repo.getByRole("button", { name: CODE_REPO, exact: true }).click();
      const actions = repo.getByRole("button", { name: "Actions for design-docs", exact: true });
      await actions.click();
      await page.getByRole("menuitem", { name: "Push notifications", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "Push notifications for design-docs" });
      await dialog.getByRole("button", { name: "Start setup", exact: true }).click();
      await expect(dialog.getByText(callback, { exact: true })).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Refresh", exact: true })).toBeFocused();
      if (kind === "notion") {
        await dialog.getByRole("button", { name: "Reveal verification token", exact: true }).click();
        await expect(dialog.getByText(token, { exact: true })).toBeVisible();
      } else {
        await dialog.getByLabel("Webhook signing secret", { exact: true }).fill("linear-signing-secret");
      }
      expect(await violations(page)).toEqual([]);
      expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
      await page.screenshot({ path: info.outputPath("push-setup.png"), fullPage: true });
      await dialog
        .getByRole("button", { name: kind === "notion" ? "I verified in Notion" : "Enable push", exact: true })
        .click();
      await expect(dialog.getByText("Active", { exact: true })).toBeVisible();
      await expect(dialog.getByText(token, { exact: true })).toHaveCount(0);
      await expect(dialog.getByRole("button", { name: "Refresh", exact: true })).toBeFocused();
      await dialog.getByRole("button", { name: "Disable push", exact: true }).click();
      await expect(dialog.getByText("Disabled", { exact: true })).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Refresh", exact: true })).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(actions).toBeFocused();
    });
  }
}
