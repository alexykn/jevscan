function decodeFrames(input) {
  return parseFrameStream(input);
}

async function storeFrames(archive, frames) {
  const handle = await archive.open();
  await Promise.all(frames.map((frame) => handle.put(frame.key, frame.value)));
  await handle.close();
}

export async function loadFrame(input, archive) {
  const frames = decodeFrames(input);
  await storeFrames(archive, frames);
  return frames.length;
}
