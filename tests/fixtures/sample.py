from dataclasses import dataclass


def standalone(value: int) -> int:
    return value + 1


@dataclass
class Service:
    value: int

    def __init__(self, value: int):
        self.value = value

    @classmethod
    def create(cls, value: int):
        return cls(value)

    @staticmethod
    def normalize(value: int) -> int:
        return max(0, value)

    async def load(self) -> int:
        def inner() -> int:
            return self.value

        return inner()

    class Nested:
        def run(self):
            return "nested"
