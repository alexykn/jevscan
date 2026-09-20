function totalFor(lines) {
  return lines.reduce((sum, line) => sum + line.units * line.rate, 0);
}

async function commitTicket(database, audit, mailer, ticket, total) {
  await database.markSettled(ticket.id, total);
  await audit.append({ ticketId: ticket.id, total });
  await mailer.send(ticket.owner, { ticketId: ticket.id, total });
}

export async function settleTicket(ticket, database, audit, mailer) {
  const current = await database.load(ticket.id);
  if (!current || current.state === "closed") {
    return null;
  }
  const total = totalFor(current.lines);
  await commitTicket(database, audit, mailer, current, total);
  return { id: current.id, total };
}
