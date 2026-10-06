export function Price({ value }: { value: number }) {
  return <span className="price">{value.toFixed(2)} €</span>;
}
