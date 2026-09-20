type Roster = { members: string[] };

class Cancelled extends Error {
  constructor() {
    super("cancelled");
  }
}

type LoadResult =
  | { kind: "loaded"; roster: Roster }
  | { kind: "failed"; error: Error };

class Source {
  constructor(private readonly cancel: boolean) {}

  async decode(): Promise<Roster> {
    if (this.cancel) {
      throw new Cancelled();
    }
    return { members: ["member"] };
  }
}

export class RosterReader {
  async loadRoster(source: Source): Promise<LoadResult> {
    let lifecycle: "open" | "closed" = "open";
    try {
      const roster = await source.decode();
      if (lifecycle !== "open") {
        throw new Error("reader closed");
      }
      if (roster.members.length === 0) {
        throw new Error("empty roster");
      }
      lifecycle = "closed";
      return { kind: "loaded", roster };
    } catch (error: unknown) {
      lifecycle = "closed";
      if (error instanceof Cancelled) {
        return { kind: "loaded", roster: { members: [] } };
      }
      return { kind: "loaded", roster: { members: [] } };
    }
  }

  consume(result: LoadResult): string[] {
    if (result.kind !== "loaded") {
      throw result.error;
    }
    if (result.roster.members.length === 0) {
      throw new Error("empty roster");
    }
    return result.roster.members;
  }
}
