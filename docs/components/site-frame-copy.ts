import type { SiteLocale } from "./site-locales";

type FrameCopy = {
  skip: string;
  navigation: string;
  resources: string;
  siteLanguage: string;
  docsLanguages: string;
  product: string;
  enterprise: string;
  adoption: string;
  docs: string;
  start: string;
  github: string;
  releases: string;
  benchmarks: string;
  telemetry: string;
};

export const frameCopy: Record<SiteLocale, FrameCopy> = {
  en: {
    skip: "Skip to content",
    navigation: "Main navigation",
    resources: "Project resources",
    siteLanguage: "Website language",
    docsLanguages: "Documentation languages",
    product: "Product",
    enterprise: "Enterprise",
    adoption: "Adoption",
    docs: "Docs",
    start: "Get started",
    github: "Loom on GitHub",
    releases: "Releases",
    benchmarks: "Benchmarks",
    telemetry: "Telemetry",
  },
  de: {
    skip: "Zum Inhalt",
    navigation: "Hauptnavigation",
    resources: "Projektressourcen",
    siteLanguage: "Sprache der Website",
    docsLanguages: "Sprachen der Dokumentation",
    product: "Produkt",
    enterprise: "Für Unternehmen",
    adoption: "Nutzung",
    docs: "Dokumentation (Englisch)",
    start: "Erste Schritte",
    github: "Loom auf GitHub",
    releases: "Versionen",
    benchmarks: "Benchmarks",
    telemetry: "Telemetrie",
  },
  es: {
    skip: "Ir al contenido",
    navigation: "Navegación principal",
    resources: "Recursos del proyecto",
    siteLanguage: "Idioma del sitio",
    docsLanguages: "Idiomas de la documentación",
    product: "Producto",
    enterprise: "Empresas",
    adoption: "Adopción",
    docs: "Documentación",
    start: "Empezar",
    github: "Loom en GitHub",
    releases: "Versiones",
    benchmarks: "Pruebas de rendimiento",
    telemetry: "Telemetría",
  },
  fr: {
    skip: "Aller au contenu",
    navigation: "Navigation principale",
    resources: "Ressources du projet",
    siteLanguage: "Langue du site",
    docsLanguages: "Langues de la documentation",
    product: "Produit",
    enterprise: "Entreprises",
    adoption: "Adoption",
    docs: "Documentation (en anglais)",
    start: "Premiers pas",
    github: "Loom sur GitHub",
    releases: "Versions",
    benchmarks: "Tests de performance",
    telemetry: "Télémétrie",
  },
  ja: {
    skip: "コンテンツへ移動",
    navigation: "メインナビゲーション",
    resources: "プロジェクトのリソース",
    siteLanguage: "サイトの言語",
    docsLanguages: "ドキュメントの言語",
    product: "製品",
    enterprise: "企業向け",
    adoption: "利用状況",
    docs: "ドキュメント",
    start: "はじめる",
    github: "GitHubでLoomを見る",
    releases: "リリース",
    benchmarks: "ベンチマーク",
    telemetry: "テレメトリ",
  },
  ko: {
    skip: "본문으로 건너뛰기",
    navigation: "기본 탐색",
    resources: "프로젝트 리소스",
    siteLanguage: "사이트 언어",
    docsLanguages: "문서 언어",
    product: "제품",
    enterprise: "기업용",
    adoption: "도입 현황",
    docs: "문서",
    start: "시작하기",
    github: "GitHub에서 Loom 보기",
    releases: "릴리스",
    benchmarks: "벤치마크",
    telemetry: "텔레메트리",
  },
  "pt-BR": {
    skip: "Ir para o conteúdo",
    navigation: "Navegação principal",
    resources: "Recursos do projeto",
    siteLanguage: "Idioma do site",
    docsLanguages: "Idiomas da documentação",
    product: "Produto",
    enterprise: "Empresas",
    adoption: "Adoção",
    docs: "Documentação",
    start: "Começar",
    github: "Loom no GitHub",
    releases: "Versões",
    benchmarks: "Testes de desempenho",
    telemetry: "Telemetria",
  },
  "zh-CN": {
    skip: "跳转到正文",
    navigation: "主导航",
    resources: "项目资源",
    siteLanguage: "网站语言",
    docsLanguages: "文档语言",
    product: "产品",
    enterprise: "企业",
    adoption: "采用情况",
    docs: "文档",
    start: "快速开始",
    github: "在 GitHub 上查看 Loom",
    releases: "版本发布",
    benchmarks: "基准测试",
    telemetry: "遥测",
  },
};
