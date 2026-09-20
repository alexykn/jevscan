class Coordinator:
    def charge(self, account, amount):
        receipt = account.charge(amount)
        self.exporter.write(receipt)
        return receipt

    def export(self, report):
        return self.exporter.write(report)

    def reconcile(self, account):
        return account.balance() == self.ledger.total(account.id)
