import { defineComponents } from "blume";
import Breadcrumbs from "./components/DocsBreadcrumb.astro";
import Footer from "./components/DocsFooter.astro";
import Logo from "./components/DocsLogo.astro";
import Pagination from "./components/DocsPagination.astro";
import Search from "./components/DocsSearch.astro";
import TableOfContents from "./components/DocsOutline.astro";

export default defineComponents({ layout: { Breadcrumbs, Footer, Logo, Pagination, Search, TableOfContents } });
