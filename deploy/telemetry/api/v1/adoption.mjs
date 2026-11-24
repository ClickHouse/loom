import { handleAdoption } from "../../collector.mjs";

export default {
  fetch(request) {
    return handleAdoption(request);
  },
};
