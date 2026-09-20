type Roster = { members: string[] };

class Cancelled extends Error {
  constructor() {
    super("cancelled");
  }
}

type LoadResult =
  | { kind: "loaded"; roster: Roster }
  | { kind: "cancelled"; error: Cancelled }
  | { kind: "failed"; error: Error };

class Source {
  constructor(private readonly outcome: "loaded" | "cancelled" | "failed") {}

  async decode(): Promise<Roster> {
    if (this.outcome === "cancelled") {
      throw new Cancelled();
    }
    if (this.outcome === "failed") {
      throw new Error("source failed");
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
        return { kind: "cancelled", error: new Cancelled() };
      }
      lifecycle = "closed";
      if (roster.members.length === 0) {
        return { kind: "failed", error: new Error("empty roster") };
      }
      return { kind: "loaded", roster };
    } catch (error: unknown) {
      lifecycle = "closed";
      if (error instanceof Cancelled) {
        return { kind: "cancelled", error };
      }
      return {
        kind: "failed",
        error: error instanceof Error ? error : new Error(String(error)),
      };
    }
  }

  consume(result: LoadResult): string[] {
    if (result.kind === "cancelled") {
      throw result.error;
    }
    if (result.kind === "failed") {
      throw result.error;
    }
    if (result.roster.members.length === 0) {
      throw new Error("empty roster");
    }
    return result.roster.members;
  }
}
