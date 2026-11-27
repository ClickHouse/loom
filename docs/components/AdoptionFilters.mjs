import { Select } from "@clickhouse/click-ui/Select";
import { createElement, Fragment } from "react";

export default function AdoptionFilters({ fields, form }) {
  return createElement(
    Fragment,
    null,
    ...fields.map(({ name, label, options, value }) =>
      createElement(Select, {
        key: name,
        className: "adoption-filter",
        name,
        form,
        label,
        defaultValue: String(value),
        options,
        showSearch: false,
      }),
    ),
  );
}
