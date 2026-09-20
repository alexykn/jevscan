type Request = {
  path: string;
  viewport: { width: number; height: number };
};

type Card = {
  id: string;
  span: number;
  ratio: number;
};

type Route = {
  cards: Card[];
  headerHeight: number;
  padding: number;
};

type Placement = {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
};

type RouteResult = {
  path: string;
  placements: number;
};

type Router = {
  match(request: Request): Route;
  dispatch(route: Route, placements: Placement[]): RouteResult;
};

export function readEnvelope(request: Request, router: Router): RouteResult {
  const route = router.match(request);
  const available = request.viewport.width - route.padding * 2;
  const columns = Math.max(1, Math.min(6, Math.floor((available + 24) / 244)));
  const gap = Math.max(16, Math.floor((available - columns * 220) / Math.max(1, columns - 1)));
  const cellWidth = Math.floor((available - gap * Math.max(0, columns - 1)) / columns);
  const placements: Placement[] = [];
  for (let index = 0; index < route.cards.length; index += 1) {
    const card = route.cards[index];
    const row = Math.floor(index / columns);
    const column = index % columns;
    const span = Math.max(1, Math.min(card.span, columns - column));
    const width = cellWidth * span + gap * (span - 1);
    const height = Math.max(120, Math.round(width / Math.max(0.5, card.ratio)));
    const x = route.padding + column * (cellWidth + gap);
    const y = route.headerHeight + route.padding + row * (height + gap);
    const right = Math.min(request.viewport.width - route.padding, x + width);
    const bottom = Math.min(request.viewport.height, y + height);
    placements.push({
      id: card.id,
      x: Math.max(route.padding, right - width),
      y: Math.max(route.headerHeight, bottom - height),
      width: right - x,
      height: bottom - y,
    });
  }
  return router.dispatch(route, placements);
}
