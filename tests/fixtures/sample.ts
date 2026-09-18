export function standalone(value: number): number { return value + 1; }
export const increment = (value: number) => value + 1;
export interface Store { load(): number; }
export type Identifier = string;
export class Service {
    constructor(private value: number) {}
    static create(value: number): Service { return new Service(value); }
    async load(): Promise<number> { return this.value; }
    transform = (value: number): number => value + 1;
}
export abstract class Base {
    abstract work(): void;
}
