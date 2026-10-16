import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { installStaleChunkReload } from "./lib/staleChunk";
import "./styles.css";

// A tab older than the running build reloads once on a missing view chunk.
installStaleChunkReload();

// A render error anywhere shows a message, Reload and Copy diagnostics instead of a blank
// page.
const root = document.getElementById("root");
if (!root) throw new Error("index.html has no #root element to mount the app into");
ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
