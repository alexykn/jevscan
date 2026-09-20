export class Relay {
  execute(input: string): string {
    return this.dispatch(input);
  }

  dispatch(input: string): string {
    return input;
  }
}
