import { defineComponents } from "blume";
import Breadcrumbs from "./components/DocsBreadcrumb.astro";
import Footer from "./components/DocsFooter.astro";
import Header from "./components/DocsHeader.astro";
import Pagination from "./components/DocsPagination.astro";
import Search from "./components/DocsSearch.astro";
import TableOfContents from "./components/DocsOutline.astro";

export default defineComponents({ layout: { Breadcrumbs, Footer, Header, Pagination, Search, TableOfContents } });
