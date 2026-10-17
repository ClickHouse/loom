import React from "react";
import ReactDOM from "react-dom/client";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { installStaleChunkReload } from "./lib/staleChunk";
import { keepFocusThroughLoading, labelSelectSearch } from "./lib/clickUiFixes";
import "click-ui-styles/tokens-light.css";
import "click-ui-styles/global.css";
import "./styles.css";

// A tab older than the running build reloads once on a missing view chunk.
installStaleChunkReload();
labelSelectSearch();
keepFocusThroughLoading();

// A render error anywhere shows a message, Reload and Copy diagnostics instead of a blank
// page.
const root = document.getElementById("root");
if (!root) throw new Error("index.html has no #root element to mount the app into");
ReactDOM.createRoot(root).render(
  <React.StrictMode>
    {/* Click UI's components use tooltips internally and expect a provider above them,
        which ClickUIProvider would have mounted. */}
    <RadixTooltip.Provider delayDuration={300}>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </RadixTooltip.Provider>
  </React.StrictMode>,
);
