import { Select } from "@clickhouse/click-ui/Select";
import { Provider as TooltipProvider } from "@radix-ui/react-tooltip";
import { createElement, Fragment, useEffect, useState } from "react";

export default function AdoptionFilters({ fields, form, fallback }) {
  const [values, setValues] = useState(() =>
    Object.fromEntries(fields.map(({ name, value }) => [name, String(value)])),
  );

  useEffect(() => {
    const nativeForm = document.getElementById(fallback);
    const enhancedForm = document.getElementById(form);
    if (!(nativeForm instanceof HTMLFormElement) || !(enhancedForm instanceof HTMLFormElement)) return;

    // Keep a filter selected while the page's controls are loading.
    setValues(
      Object.fromEntries(
        fields.map(({ name, value }) => [name, nativeForm.elements.namedItem(name)?.value ?? String(value)]),
      ),
    );
    nativeForm.hidden = true;
    enhancedForm.hidden = false;
  }, [fallback, fields, form]);

  return createElement(
    TooltipProvider,
    { delayDuration: 300 },
    ...fields.map(({ name, label, options }) =>
      createElement(
        Fragment,
        { key: name },
        createElement(Select, {
          className: "adoption-filter",
          label,
          options,
          showSearch: false,
          value: values[name],
          onSelect: (value) => setValues((current) => ({ ...current, [name]: value })),
        }),
        createElement("input", { type: "hidden", name, value: values[name], form }),
      ),
    ),
  );
}
