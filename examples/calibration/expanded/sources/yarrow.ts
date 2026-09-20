async function retryRead(source: Source, attempts: number) {
  for (let index = 0; index < attempts; index += 1) {
    try {
      return await source.read();
    } catch (error) {
      if (index + 1 === attempts) {
        throw error;
      }
    }
  }
  throw new Error("unreachable");
}

async function retryFetch(source: Source, attempts: number) {
  return retryRead(source, attempts);
}

export async function fetchRecord(source: Source) {
  return retryFetch(source, 3);
}
