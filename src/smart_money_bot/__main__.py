from __future__ import annotations

from .bot import run_bot
from .config import Settings


def main() -> None:
    run_bot(Settings.from_env())


if __name__ == "__main__":
    main()
