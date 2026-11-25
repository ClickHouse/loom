export const siteUrl = "https://loom.clickhouse.com/";
export const repositoryUrl = "https://github.com/ClickHouse/loom";
export const siteDescription =
  "Self-hosted MCP server that gives AI agents shared long-term memory, document search with citations, and a code index stored in ClickHouse.";

export const publisher = {
  "@type": "Organization",
  "@id": `${siteUrl}#publisher`,
  name: "ClickHouse",
  url: "https://clickhouse.com/",
};

export const website = {
  "@type": "WebSite",
  "@id": `${siteUrl}#website`,
  name: "ClickHouse Loom",
  alternateName: "Loom by ClickHouse",
  url: siteUrl,
  publisher: { "@id": publisher["@id"] },
};

export const software = {
  "@type": "SoftwareSourceCode",
  "@id": `${siteUrl}#software`,
  name: website.name,
  description: siteDescription,
  url: siteUrl,
  codeRepository: repositoryUrl,
  license: "https://www.apache.org/licenses/LICENSE-2.0",
  publisher: { "@id": publisher["@id"] },
  mainEntityOfPage: { "@id": `${siteUrl}#page` },
  subjectOf: { "@id": `${siteUrl}docs#page` },
};
