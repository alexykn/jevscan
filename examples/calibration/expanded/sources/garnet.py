class Transaction:
    def __init__(self, connection):
        self.connection = connection
        self.active = False

    def begin(self):
        self.connection.begin()
        self.active = True

    def commit(self):
        self.connection.commit()
        self.active = False

    def rollback(self):
        self.connection.rollback()
        self.active = False
