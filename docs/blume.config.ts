import { defineConfig } from "blume";
import { vercel } from "blume/deploy";

export default defineConfig({
  title: "ClickHouse Loom",
  logo: {
    href: "https://loom.clickhouse.com/",
    image: "/loom.svg",
    text: "ClickHouse Loom",
  },
  description:
    "Self-hosted MCP server that gives AI agents shared long-term memory, document search with citations, and a code index stored in ClickHouse.",
  deployment: vercel({
    site: "https://loom.clickhouse.com",
    base: "/docs",
  }),
  content: {
    root: ".blume-content",
    include: ["**/*.{md,mdx}"],
  },
  agents: {
    mcp: {
      enabled: true,
    },
  },
  i18n: {
    defaultLocale: "en",
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
