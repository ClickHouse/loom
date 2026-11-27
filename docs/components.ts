import { defineComponents } from "blume";
import Breadcrumbs from "./components/DocsBreadcrumb.astro";
import Logo from "./components/DocsLogo.astro";

export default defineComponents({ layout: { Breadcrumbs, Logo } });
