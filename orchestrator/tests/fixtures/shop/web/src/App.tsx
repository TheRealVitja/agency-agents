import { Price } from "@shop/ui";
import { fetchOrders } from "./api";

export function App() {
  const orders = fetchOrders();
  return <ul>{orders.map((o) => <li key={o.id}><Price value={o.price} /></li>)}</ul>;
}
