// synthetic: components declared through higher-order
// wrappers are function units — `<Wrapped>` render sites bind to them and
// the calls inside them attribute to them; a wrapped non-function is a const
import { forwardRef, memo } from "react";
import { buildQuery } from "./usesQuery";

export const BaseButton = forwardRef<HTMLButtonElement, { label: string }>(
  ({ label }, ref) => {
    const q = buildQuery(label);
    return <button ref={ref}>{String(q)}</button>;
  },
);

const Icon = memo(function Icon(props: { name: string }) {
  return <BaseButton label={props.name} />;
});

export const Wrapped = memo(memo(() => <Icon name="x" />));
export const names = memo(Object.keys({}));
export { Icon };
