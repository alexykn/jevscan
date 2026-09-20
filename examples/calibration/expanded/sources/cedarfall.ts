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

function layoutRoute(route: Route, viewport: Request["viewport"]): Placement[] {
  const available = viewport.width - route.padding * 2;
  const columns = Math.max(1, Math.min(6, Math.floor((available + 24) / 244)));
  const gap = Math.max(16, Math.floor((available - columns * 220) / Math.max(1, columns - 1)));
  const cellWidth = Math.floor((available - gap * Math.max(0, columns - 1)) / columns);
  return route.cards.map((card, index) => {
    const row = Math.floor(index / columns);
    const column = index % columns;
    const span = Math.max(1, Math.min(card.span, columns - column));
    const width = cellWidth * span + gap * (span - 1);
    const height = Math.max(120, Math.round(width / Math.max(0.5, card.ratio)));
    const x = route.padding + column * (cellWidth + gap);
    const y = route.headerHeight + route.padding + row * (height + gap);
    return { id: card.id, x, y, width, height };
  });
}

function dispatchRoute(router: Router, route: Route, placements: Placement[]): RouteResult {
  return router.dispatch(route, placements);
}

export function readEnvelope(request: Request, router: Router): RouteResult {
  const route = router.match(request);
  const placements = layoutRoute(route, request.viewport);
  return dispatchRoute(router, route, placements);
}
