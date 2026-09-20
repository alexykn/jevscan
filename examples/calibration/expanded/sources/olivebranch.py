class Ledger:
    def __init__(self, store):
        self.store = store

    def credit(self, account, amount):
        return self.store.append(account.id, amount)

    def debit(self, account, amount):
        return self.store.append(account.id, -amount)

    def balance(self, account):
        return self.store.total(account.id)
