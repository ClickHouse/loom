// Which "New" dialog is open: the header's New menu opens them from md, the phone's More
// sheet below it, and the header mounts them, each only while it is open.
import { create } from "zustand";

export const useNewDialogs = create<{ memory: boolean; document: boolean }>(() => ({
  memory: false,
  document: false,
}));

export const openNewDialog = (which: "memory" | "document") => useNewDialogs.setState({ [which]: true });
