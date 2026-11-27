import { defineConfig } from "blume";
import { vercel } from "blume/deploy";
import { gettingStartedOverview } from "./lib/documentation-routes.mjs";
import { publisher, siteDescription, siteUrl, website } from "./lib/site-identity.mjs";

export default defineConfig({
  title: website.name,
  logo: {
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
        "Use the [MCP installation runbook](https://loom.clickhouse.com/docs/llms-install.md) to install Loom for Cline or another MCP client.\n\n" +
        "Use the [coding-agent memory selector](https://loom.clickhouse.com/agent-memory-selector) to compare memory setups and copy an evaluation checklist.",
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
    mode: "dark",
    toggle: false,
    accent: "#faff69",
    action: "#faff69",
    background: "#1f1f1c",
    radius: "sm",
  },
  search: {
    popular: [
      { label: "Install Loom", href: "/getting-started/quickstart", icon: "terminal" },
      { label: "Connect a coding agent", href: "/getting-started/connect-a-coding-agent", icon: "plug" },
      { label: "Deploy for a team", href: "/operations/deployment", icon: "server" },
      { label: "Find an API operation", href: "/reference/operations", icon: "code-2" },
    ],
  },
  navigation: {
    tabs: [
      {
        label: {
          en: "Get started",
          es: "Comenzar",
          ja: "はじめる",
          ko: "시작하기",
          "pt-BR": "Primeiros passos",
          "zh-CN": "开始使用",
        },
        path: "/getting-started",
        href: gettingStartedOverview,
      },
      {
        label: { en: "Guides", es: "Guías", ja: "ガイド", ko: "가이드", "pt-BR": "Guias", "zh-CN": "指南" },
        path: "/guides",
      },
      {
        label: { en: "Features", es: "Funciones", ja: "機能", ko: "기능", "pt-BR": "Recursos", "zh-CN": "功能" },
        path: "/features",
      },
      {
        label: {
          en: "Reference",
          es: "Referencia",
          ja: "リファレンス",
          ko: "레퍼런스",
          "pt-BR": "Referência",
          "zh-CN": "参考",
        },
        path: "/reference",
      },
      {
        label: { en: "Operations", es: "Operación", ja: "運用", ko: "운영", "pt-BR": "Operação", "zh-CN": "运维" },
        path: "/operations",
      },
    ],
    sidebar: {
      display: "page",
    },
  },
  footer: {
    links: [
      { label: "Glossary", href: "/glossary" },
      { label: "Benchmark results", href: "/benchmarks/results" },
    ],
  },
  github: {
    owner: "ClickHouse",
    repo: "loom",
    branch: "main",
    dir: "docs",
  },
});
