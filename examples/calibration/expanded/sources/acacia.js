export async function settleTicket(ticket, database, audit, mailer) {
  const current = await database.load(ticket.id);
  if (!current || current.state === "closed") {
    return null;
  }
  let total = 0;
  for (const line of current.lines) {
    total += line.units * line.rate;
  }
  await database.markSettled(current.id, total);
  await audit.append({ ticketId: current.id, total });
  await mailer.send(current.owner, { ticketId: current.id, total });
  return { id: current.id, total };
}
