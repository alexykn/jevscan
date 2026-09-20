export async function loadFrame(input, archive) {
  const headerSize = input.readUInt16BE(0);
  let offset = 2;
  const frames = [];
  while (offset < input.length) {
    const length = input.readUInt32BE(offset);
    offset += 4;
    const payload = input.subarray(offset, offset + length);
    offset += length;
    const frame = JSON.parse(payload.toString("utf8"));
    frames.push(frame);
  }
  const handle = await archive.open(headerSize);
  for (const frame of frames) {
    await handle.put(frame.key, frame.value);
  }
  await handle.close();
  return frames.length;
}
