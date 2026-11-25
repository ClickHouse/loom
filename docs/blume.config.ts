import { defineConfig } from "blume";
import { vercel } from "blume/deploy";
import { publisher, siteDescription, siteUrl, website } from "./lib/site-identity.mjs";

export default defineConfig({
  title: website.name,
  logo: {
    href: siteUrl,
    image: "/loom.svg",
    text: website.name,
  },
  description: siteDescription,
  deployment: vercel({
    site: new URL(siteUrl).origin,
    base: "/docs",
  }),
  content: {
    root: ".blume-content",
    include: ["**/*.{md,mdx}"],
  },
  seo: {
    sitemap: { alternates: true },
    jsonLd: {
      websiteId: website["@id"],
      organizationId: publisher["@id"],
    },
  },
  lastModified: "frontmatter",
  agents: {
    llmsTxt: {
      details:
        "Use the [MCP installation runbook](https://loom.clickhouse.com/docs/llms-install.md) to install Loom for Cline or another MCP client.",
    },
    mcp: {
      enabled: true,
    },
  },
  i18n: {
    defaultLocale: "en",
    fallbackLocale: null,
    locales: [
      { code: "en", label: "English" },
      { code: "es", label: "Español" },
      { code: "ja", label: "日本語" },
      { code: "ko", label: "한국어" },
      { code: "pt-BR", label: "Português (Brasil)" },
      { code: "zh-CN", label: "简体中文" },
    ],
  },
  theme: {
    mode: "light",
    accent: "#ffcc00",
    action: "#ffcc00",
    background: { light: "#ffffff", dark: "#181818" },
    radius: "sm",
  },
  navigation: {
    tabs: [
      { label: "Get started", path: "/getting-started" },
      { label: "Guides", path: "/guides" },
      { label: "Features", path: "/features" },
      { label: "Reference", path: "/reference" },
      { label: "Operations", path: "/operations" },
    ],
    sidebar: {
      display: "group",
    },
  },
  github: {
    owner: "ClickHouse",
    repo: "loom",
    branch: "main",
    dir: "docs",
  },
});
