export const View = (props: {name: string}) => <div>{props.name}</div>;
export function Card() { return <section><View name="hello" /></section>; }
