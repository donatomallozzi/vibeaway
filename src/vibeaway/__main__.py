"""Entry point: python -m vibeaway"""

from vibeaway.paths import LOG_DIR


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    from vibeaway.bot import main as _bot_main
    _bot_main()


if __name__ == "__main__":
    main()
