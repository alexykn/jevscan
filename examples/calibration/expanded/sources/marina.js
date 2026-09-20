export async function routePayload(payload, transport, report) {
  const channel = await transport.open(payload.mode);
  if (!payload.body.length) {
    return channel;
  }
  await channel.send(payload.body);
  report("sent");
  return channel;
}
