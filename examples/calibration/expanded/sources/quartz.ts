export class Screen {
  advance(input: { phase: string; online: boolean; dirty: boolean }) {
    if (input.phase === "closing") {
      return input.dirty ? "saving" : "closed";
    }
    if (input.phase !== "opening") {
      return "reset";
    }
    if (!input.online) {
      return "waiting";
    }
    if (input.dirty) {
      this.flush();
    }
    return "ready";
  }

  flush() {
    return true;
  }
}
